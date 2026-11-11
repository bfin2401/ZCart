// Runs the action callbacks against a real studio-bridge in --mock mode.
const test = require('node:test')
const assert = require('node:assert')
const { spawn } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const os = require('node:os')
const { BridgeClient } = require('../src/bridge.js')
const { buildActions, buildFeedbacks, buildVariables, buildPresets } = require('../src/definitions.js')

const python = process.env.BRIDGE_PYTHON || path.resolve(__dirname, '../../../.venv/bin/python')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function withBridge(port, extraConfig, body) {
	const config = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'sb-')), 'config.toml')
	fs.writeFileSync(config, `[http]\nport = ${port}\n${extraConfig}`)
	const bridge = spawn(python, ['-m', 'studio_bridge', '-c', config, 'run', '--mock'], { stdio: 'ignore' })
	const ctx = { state: null }
	const client = new BridgeClient({ host: '127.0.0.1', port, onStatus: () => {}, onState: (s) => (ctx.state = s) })
	ctx.until = async (check) => {
		for (let i = 0; i < 150; i++) {
			if (ctx.state && check(ctx.state.evo)) return
			await sleep(20)
		}
		assert.fail('state never arrived')
	}
	ctx.post = (p, b) => client.post(p, b)
	client.start()
	try {
		await ctx.until((evo) => evo.available)
		await body(ctx)
	} finally {
		client.stop()
		bridge.kill()
	}
}

const SMARTGAIN = `
[evo.smartgain]
assume_running_s = 0.6
[[evo.smartgain.start]]
value = "0x0300 + ch"
index = 0x3A00
data = "01000000"
per_channel = true
[[evo.smartgain.cancel]]
value = 0x0300
index = 0x3A00
data = "00000000"
`

test('smartgain, once configured in the bridge', async () => {
	await withBridge(18766, SMARTGAIN, async (ctx) => {
		const actions = buildActions(ctx.post)
		const feedbacks = buildFeedbacks(() => ctx.state)
		const running = (channel) => feedbacks.smartgain_running.callback({ options: { channel } })
		assert.strictEqual(feedbacks.smartgain_unconfigured.callback({ options: {} }), false)
		assert.strictEqual(running(0), false)

		await actions.smartgain_start.callback({ options: { channels: [2, 3] } })
		await ctx.until((evo) => evo.smartgain.running)
		assert.strictEqual(running(0), true)
		assert.strictEqual(running(2), true)
		assert.strictEqual(running(1), false)
		assert.strictEqual(buildVariables(ctx.state).values.smartgain, 'Listening')
		// the two per-channel transfers reached the (mock) device
		const got = await ctx.post('/api/evo/raw', { request_type: 0xa1, value: 0x0302, index: 0x3a00 })
		assert.strictEqual(got.data, '01000000')

		// it stops showing as running by itself ...
		await ctx.until((evo) => !evo.smartgain.running)
		// ... or when cancelled
		await actions.smartgain_start.callback({ options: { channels: [1] } })
		await actions.smartgain_cancel.callback({ options: {} })
		await ctx.until((evo) => !evo.smartgain.running)
	})
})

test('before setup, smartgain says what is missing; the other controls work', async () => {
	await withBridge(18767, '', async (ctx) => {
		const feedbacks = buildFeedbacks(() => ctx.state)
		assert.strictEqual(feedbacks.smartgain_unconfigured.callback({ options: {} }), true)
		await assert.rejects(ctx.post('/api/evo/smartgain', { action: 'start', channels: [1] }), /not configured yet/)

		const actions = buildActions(ctx.post)
		await actions.phantom.callback({ options: { channel: 2, state: 'toggle' } })
		await actions.gain_adjust.callback({ options: { channel: 1, delta: -3 } })
		await actions.mute.callback({ options: { channel: 4, state: 'on' } })
		await ctx.until((evo) => evo.inputs[1].phantom && evo.inputs[0].gain_db === 17 && evo.inputs[3].mute)
		assert.strictEqual(feedbacks.phantom.callback({ options: { channel: 2 } }), true)
		assert.strictEqual(feedbacks.phantom.callback({ options: { channel: 1 } }), false)
		assert.strictEqual(feedbacks.mute.callback({ options: { channel: 4 } }), true)
		const { values } = buildVariables(ctx.state)
		assert.strictEqual(values.gain_1, 17)
		assert.strictEqual(values.phantom_2, 'On')
		assert.strictEqual(values.smartgain, 'Not set up')
	})
})

test('variables and presets refer to things that exist', () => {
	const { definitions, values } = buildVariables(null)
	assert.deepStrictEqual(Object.keys(values).sort(), definitions.map((d) => d.variableId).sort())
	const actions = buildActions(() => {})
	const feedbacks = buildFeedbacks(() => null)
	for (const preset of Object.values(buildPresets('evo'))) {
		for (const step of preset.steps)
			for (const list of Object.values(step))
				for (const action of list) {
					assert.ok(actions[action.actionId], action.actionId)
					for (const option of actions[action.actionId].options) assert.ok(option.id in action.options)
				}
		for (const fb of preset.feedbacks) assert.ok(feedbacks[fb.feedbackId], fb.feedbackId)
		for (const [, name] of preset.style.text.matchAll(/\$\(evo:([^)]+)\)/g)) assert.ok(name in values, name)
	}
	assert.strictEqual(feedbacks.unavailable.callback({ options: {} }), true)
})
