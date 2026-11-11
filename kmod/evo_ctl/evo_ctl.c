// SPDX-License-Identifier: GPL-2.0
/*
 * evo_ctl - audio class control requests for the Audient EVO 8 (and EVO 4)
 *
 * usbfs refuses class requests addressed to an interface that a kernel driver
 * holds, and snd-usb-audio holds the EVO's Audio Control interface. This
 * module binds the EVO's otherwise unused DFU interface (number 3) only to get
 * hold of the device, and offers one ioctl on /dev/evo8 (or /dev/evo4) that
 * forwards a control transfer on endpoint 0. Audio streaming is not touched.
 *
 * Only class requests to interface 0 (Audio Control) are forwarded. Anything
 * else - DFU requests in particular, which could start a firmware download -
 * is refused.
 *
 * The device node and ioctl are the same as those of the evo_raw module from
 * the audient-evo-py project, so programs written for either work with both.
 * Do not load both at once.
 */
#include <linux/fs.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/usb.h>

#define AUDIENT_VID 0x2708
#define EVO4_PID 0x0006
#define EVO8_PID 0x0007
#define EVO_DFU_INTERFACE 3
#define EVO_MAX_DATA 256
#define EVO_TIMEOUT_MS 1000

struct evo_ctrl_xfer {
	__u8 bRequestType;
	__u8 bRequest;
	__u16 wValue;
	__u16 wIndex;
	__u16 wLength;
	__u8 data[EVO_MAX_DATA];
};

#define EVO_CTRL_TRANSFER _IOWR('E', 0, struct evo_ctrl_xfer)

struct evo_device {
	struct usb_device *udev;
	struct miscdevice misc;
	struct mutex lock;
	struct kref kref;
	char name[8];
};

static void evo_release_dev(struct kref *kref)
{
	struct evo_device *dev = container_of(kref, struct evo_device, kref);

	kfree(dev);
}

static int evo_open(struct inode *inode, struct file *file)
{
	/* misc_open() left the miscdevice in private_data */
	struct evo_device *dev =
		container_of(file->private_data, struct evo_device, misc);

	kref_get(&dev->kref);
	file->private_data = dev;
	return 0;
}

static int evo_release(struct inode *inode, struct file *file)
{
	struct evo_device *dev = file->private_data;

	kref_put(&dev->kref, evo_release_dev);
	return 0;
}

static long evo_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct evo_device *dev = file->private_data;
	struct evo_ctrl_xfer *xfer;
	unsigned int pipe;
	bool in;
	int ret;

	if (cmd != EVO_CTRL_TRANSFER)
		return -ENOTTY;

	xfer = memdup_user((void __user *)arg, sizeof(*xfer));
	if (IS_ERR(xfer))
		return PTR_ERR(xfer);

	ret = -EINVAL;
	if (xfer->wLength > EVO_MAX_DATA)
		goto out;
	/* class request, recipient interface, Audio Control interface only */
	ret = -EPERM;
	if ((xfer->bRequestType & ~USB_DIR_IN) !=
	    (USB_TYPE_CLASS | USB_RECIP_INTERFACE))
		goto out;
	if ((xfer->wIndex & 0xff) != 0)
		goto out;

	in = xfer->bRequestType & USB_DIR_IN;

	mutex_lock(&dev->lock);
	if (!dev->udev) {
		mutex_unlock(&dev->lock);
		ret = -ENODEV;
		goto out;
	}
	pipe = in ? usb_rcvctrlpipe(dev->udev, 0) : usb_sndctrlpipe(dev->udev, 0);
	/* xfer is kmalloc'ed, so its data member is fit for DMA */
	ret = usb_control_msg(dev->udev, pipe, xfer->bRequest, xfer->bRequestType,
			      xfer->wValue, xfer->wIndex, xfer->data,
			      xfer->wLength, EVO_TIMEOUT_MS);
	mutex_unlock(&dev->lock);
	if (ret < 0)
		goto out;

	if (in) {
		xfer->wLength = ret;
		if (copy_to_user((void __user *)arg, xfer, sizeof(*xfer)))
			ret = -EFAULT;
	}
out:
	kfree(xfer);
	return ret;
}

static const struct file_operations evo_fops = {
	.owner = THIS_MODULE,
	.open = evo_open,
	.release = evo_release,
	.unlocked_ioctl = evo_ioctl,
	.compat_ioctl = compat_ptr_ioctl,
	.llseek = noop_llseek,
};

static int evo_probe(struct usb_interface *intf, const struct usb_device_id *id)
{
	struct usb_device *udev = interface_to_usbdev(intf);
	struct evo_device *dev;
	int ret;

	if (intf->cur_altsetting->desc.bInterfaceNumber != EVO_DFU_INTERFACE)
		return -ENODEV;

	dev = kzalloc(sizeof(*dev), GFP_KERNEL);
	if (!dev)
		return -ENOMEM;

	kref_init(&dev->kref);
	mutex_init(&dev->lock);
	strscpy(dev->name,
		le16_to_cpu(udev->descriptor.idProduct) == EVO4_PID ? "evo4" : "evo8",
		sizeof(dev->name));
	dev->udev = usb_get_dev(udev);
	dev->misc.minor = MISC_DYNAMIC_MINOR;
	dev->misc.name = dev->name;
	dev->misc.fops = &evo_fops;
	dev->misc.parent = &intf->dev;

	ret = misc_register(&dev->misc);
	if (ret) {
		dev_err(&intf->dev, "cannot register /dev/%s: %d\n", dev->name, ret);
		usb_put_dev(dev->udev);
		kfree(dev);
		return ret;
	}
	usb_set_intfdata(intf, dev);
	dev_info(&intf->dev, "Audient %s control at /dev/%s\n", dev->name, dev->name);
	return 0;
}

static void evo_disconnect(struct usb_interface *intf)
{
	struct evo_device *dev = usb_get_intfdata(intf);

	if (!dev)
		return;
	usb_set_intfdata(intf, NULL);
	misc_deregister(&dev->misc);

	mutex_lock(&dev->lock);
	usb_put_dev(dev->udev);
	dev->udev = NULL;
	mutex_unlock(&dev->lock);

	/* an open file keeps the structure until it is closed */
	kref_put(&dev->kref, evo_release_dev);
}

static const struct usb_device_id evo_table[] = {
	{ USB_DEVICE_INTERFACE_NUMBER(AUDIENT_VID, EVO4_PID, EVO_DFU_INTERFACE) },
	{ USB_DEVICE_INTERFACE_NUMBER(AUDIENT_VID, EVO8_PID, EVO_DFU_INTERFACE) },
	{}
};
MODULE_DEVICE_TABLE(usb, evo_table);

static struct usb_driver evo_driver = {
	.name = "evo_ctl",
	.probe = evo_probe,
	.disconnect = evo_disconnect,
	.id_table = evo_table,
};
module_usb_driver(evo_driver);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Audio class control requests for Audient EVO interfaces");
