// Runs the action callbacks against a real studio-bridge in --mock mode.
const test = require('node:test')
const assert = require('node:assert')
const { spawn } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const os = require('node:os')
const { BridgeClient } = require('../src/bridge.js')
const { buildActions, buildFeedbacks, buildVariables, buildPresets, rgbToHueSat } = require('../src/definitions.js')

const PORT = 18765
const python = process.env.BRIDGE_PYTHON || path.resolve(__dirname, '../../../.venv/bin/python')
let bridge, client, state

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const light = (id) => state.lights.lights.find((l) => l.id === id).state
async function until(check) {
	for (let i = 0; i < 400; i++) {
		if (state && check()) return
		await sleep(20)
	}
	assert.fail('state never arrived')
}

test.before(async () => {
	const config = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'sb-')), 'config.toml')
	fs.writeFileSync(config, `[http]\nport = ${PORT}\n`)
	bridge = spawn(python, ['-m', 'studio_bridge', '-c', config, 'run', '--mock'], { stdio: 'ignore' })
	client = new BridgeClient({ host: '127.0.0.1', port: PORT, onStatus: () => {}, onState: (s) => (state = s) })
	client.start()
	await until(() => state.lights.connected)
})

test.after(() => {
	client.stop()
	bridge.kill()
})

const run = (actions, id, options) => actions[id].callback({ options })
const send = (body) => client.post('/api/lights/set', body)

test('actions drive the bridge', async () => {
	const actions = buildActions(state, send)
	assert.deepStrictEqual(
		actions.power.options[0].choices.map((c) => c.id),
		['all', 'key', 'fill', 'back-l', 'back-r', 'spot'],
	)
	await run(actions, 'cct', { lights: ['key'], kelvin: 3200, gm: 10, withBrightness: true, brightness: 42 })
	await until(() => light('key').kelvin === 3200)
	assert.strictEqual(light('key').brightness, 42)
	assert.strictEqual(light('key').gm, 10)
	assert.strictEqual(light('fill').kelvin, 5600)

	await run(actions, 'brightness_adjust', { lights: ['key', 'fill'], delta: 8 })
	await until(() => light('key').brightness === 50)
	assert.strictEqual(light('fill').brightness, 58)

	await run(actions, 'color', { lights: ['all'], color: 0x00ff00, withBrightness: false })
	await until(() => light('key').mode === 'hsi')
	assert.strictEqual(light('key').hue, 120)
	assert.strictEqual(light('spot').mode, 'cct') // bi-colour: left alone

	await run(actions, 'power', { lights: ['all'], state: 'off' })
	await until(() => !light('spot').on)
	const feedbacks = buildFeedbacks(() => state)
	assert.strictEqual(feedbacks.any_on.callback({ options: {} }), false)
	await run(actions, 'power', { lights: ['spot'], state: 'toggle' })
	await until(() => light('spot').on)
	assert.strictEqual(feedbacks.light_on.callback({ options: { light: 'spot' } }), true)
	assert.strictEqual(feedbacks.light_on.callback({ options: { light: 'key' } }), false)
	assert.strictEqual(feedbacks.mode.callback({ options: { light: 'key', mode: 'hsi' } }), true)
	assert.strictEqual(feedbacks.disconnected.callback({ options: {} }), false)

	await run(actions, 'effect', { lights: ['spot'], effect: 11, speed: 30, withBrightness: false })
	await until(() => light('spot').mode === 'effect')
	assert.strictEqual(light('spot').effect, 11)
})

test('a bad request is reported, not thrown away', async () => {
	await assert.rejects(send({ targets: ['nope'], power: 'on' }), /unknown light/)
})

test('variables and presets refer to things that exist', () => {
	const { definitions, values } = buildVariables(state)
	assert.deepStrictEqual(Object.keys(values).sort(), definitions.map((d) => d.variableId).sort())
	assert.strictEqual(values.bluetooth, 'Connected')
	const actions = buildActions(state, send)
	const feedbacks = buildFeedbacks(() => state)
	const presets = buildPresets(state, 'godox')
	assert.ok(Object.keys(presets).length > 30)
	for (const preset of Object.values(presets)) {
		for (const step of preset.steps)
			for (const list of Object.values(step))
				for (const action of list) {
					const def = actions[action.actionId]
					assert.ok(def, action.actionId)
					for (const option of def.options) assert.ok(option.id in action.options, `${action.actionId}.${option.id}`)
				}
		for (const fb of preset.feedbacks) assert.ok(feedbacks[fb.feedbackId], fb.feedbackId)
		for (const [, name] of preset.style.text.matchAll(/\$\(godox:([^)]+)\)/g)) assert.ok(name in values, name)
	}
})

test('colour conversion', () => {
	assert.deepStrictEqual(rgbToHueSat(0xff0000), { hue: 0, saturation: 100 })
	assert.deepStrictEqual(rgbToHueSat(0x0000ff), { hue: 240, saturation: 100 })
	assert.deepStrictEqual(rgbToHueSat(0xffffff), { hue: 0, saturation: 0 })
	assert.deepStrictEqual(rgbToHueSat(0xff8080), { hue: 0, saturation: 50 })
	assert.deepStrictEqual(rgbToHueSat(0xff00ff), { hue: 300, saturation: 100 })
})
