// End-to-end check of a built module package: a stand-in for Companion's
// module host forks pkg/main.js, registers it, initialises it against a
// studio-bridge in --mock mode and presses an action.
//
//   node --test fake-host.test.js      (after `npm run package` in each module, and unpacking the .tgz)
const test = require('node:test')
const assert = require('node:assert')
const { fork, spawn, execFileSync } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const os = require('node:os')

const python = process.env.BRIDGE_PYTHON || path.resolve(__dirname, '../.venv/bin/python')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function unpack(module) {
	const dir = path.join(__dirname, `companion-module-studio-${module}`)
	const tgz = fs.readdirSync(dir).find((f) => f.endsWith('.tgz'))
	assert.ok(tgz, `build ${module} first: npx companion-module-build`)
	const out = fs.mkdtempSync(path.join(os.tmpdir(), `pkg-${module}-`))
	execFileSync('tar', ['xzf', path.join(dir, tgz), '-C', out])
	return path.join(out, 'pkg')
}

async function host(module, port, body) {
	const pkg = unpack(module)
	const config = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'sb-')), 'config.toml')
	fs.writeFileSync(config, `[http]\nport = ${port}\n`)
	const bridge = spawn(python, ['-m', 'studio_bridge', '-c', config, 'run', '--mock'], { stdio: 'ignore' })
	await sleep(1200)
	const child = fork(path.join(pkg, 'main.js'), [], {
		env: { ...process.env, MODULE_MANIFEST: path.join(pkg, 'companion/manifest.json'), CONNECTION_ID: 'c1', VERIFICATION_TOKEN: 't' },
		stdio: ['ignore', 'ignore', 'inherit', 'ipc'],
	})
	const seen = {}
	const pending = new Map()
	let nextId = 1
	child.on('message', (msg) => {
		if (msg.direction === 'response') {
			const p = pending.get(msg.callbackId)
			pending.delete(msg.callbackId)
			if (p) (msg.success ? p.resolve : p.reject)(msg.payload ? JSON.parse(msg.payload) : undefined)
			return
		}
		seen[msg.name] = msg.payload ? JSON.parse(msg.payload) : undefined
		if (msg.callbackId) child.send({ direction: 'response', callbackId: msg.callbackId, success: true, payload: '{}' })
	})
	const call = (name, payload) =>
		new Promise((resolve, reject) => {
			const callbackId = nextId++
			pending.set(callbackId, { resolve, reject })
			child.send({ direction: 'call', name, payload: JSON.stringify(payload), callbackId })
		})
	const until = async (check, what) => {
		for (let i = 0; i < 200; i++) {
			if (check()) return
			await sleep(25)
		}
		assert.fail(`never happened: ${what}`)
	}
	try {
		await until(() => 'register' in seen, 'register')
		await call('init', {
			label: module,
			isFirstInit: false,
			config: { host: '127.0.0.1', port: String(port) },
			lastUpgradeIndex: -1,
			actions: {},
			feedbacks: {},
		})
		await body({ seen, call, until, port })
	} finally {
		child.kill()
		bridge.kill()
	}
}

const bridgeState = async (port) => (await fetch(`http://127.0.0.1:${port}/api/state`)).json()
const action = (actionId, options) => ({
	action: { id: 'a1', controlId: 'bank:1', actionId, options },
	surfaceId: undefined,
})

test('godox package runs under a module host', async () => {
	await host('godox', 18771, async ({ seen, call, until, port }) => {
		await until(() => seen['set-status'] && seen['set-status'].status === 'ok', 'status ok')
		// definitions are rebuilt once the bridge has listed its lights
		await until(() => JSON.stringify(seen.setActionDefinitions || '').includes('back-l'), 'light choices')
		assert.ok(seen.setFeedbackDefinitions.feedbacks.some((f) => f.id === 'light_on'))
		assert.ok(seen.setPresetDefinitions.presets.some((p) => p.id === 'key_dial_brightness'))
		assert.ok(seen.setVariableDefinitions.variables.some((v) => v.id === 'spot_kelvin'))
		await call('executeAction', action('cct', { lights: ['spot'], kelvin: 3200, gm: 0, withBrightness: true, brightness: 77 }))
		await until(() => (seen.setVariableValues.newValues || []).some((v) => v.id === 'spot_brightness' && v.value === 77), 'variable update')
		const spot = (await bridgeState(port)).lights.lights.find((l) => l.id === 'spot').state
		assert.deepStrictEqual([spot.kelvin, spot.brightness], [3200, 77])
	})
})

test('evo package runs under a module host', async () => {
	await host('evo', 18772, async ({ seen, call, until, port }) => {
		await until(() => seen['set-status'] && seen['set-status'].status === 'ok', 'status ok')
		assert.ok(seen.setActionDefinitions.actions.some((a) => a.id === 'smartgain_start'))
		await call('executeAction', action('phantom', { channel: 3, state: 'on' }))
		await until(() => (seen.setVariableValues.newValues || []).some((v) => v.id === 'phantom_3' && v.value === 'On'), 'phantom variable')
		assert.strictEqual((await bridgeState(port)).evo.inputs[2].phantom, true)
		await call('executeAction', action('smartgain_start', { channels: [1] }))
		await until(() => (seen.setVariableValues.newValues || []).some((v) => v.id === 'smartgain' && v.value === 'Listening'), 'listening')
		await until(() => (seen.setVariableValues.newValues || []).some((v) => v.id === 'smartgain' && v.value === 'Idle'), 'idle again')
		// a request the bridge refuses is logged, not lost
		await call('executeAction', action('gain', { channel: 9, db: 10 }))
		await until(() => seen['log-message'] && /channel must be/.test(seen['log-message'].message), 'log message')
	})
})
