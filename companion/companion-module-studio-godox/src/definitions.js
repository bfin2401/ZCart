// Actions, feedbacks, variables and presets for the lights. Pure functions of
// the bridge state, so they can be tested without Companion.

const rgb = (r, g, b) => ((r & 0xff) << 16) | ((g & 0xff) << 8) | (b & 0xff)
const WHITE = rgb(255, 255, 255)
const BLACK = rgb(0, 0, 0)
const AMBER = rgb(255, 170, 0)
const DIM = rgb(40, 40, 40)

const ALL = 'all'

function lightsOf(state) {
	return (state && state.lights && state.lights.lights) || []
}

function lightChoices(state) {
	return [{ id: ALL, label: 'All lights' }, ...lightsOf(state).map((l) => ({ id: l.id, label: l.name }))]
}

function effectChoices(state) {
	const seen = new Map()
	for (const light of lightsOf(state)) for (const fx of light.profile.effects) seen.set(fx.id, fx.name)
	return [...seen.entries()].sort((a, b) => a[0] - b[0]).map(([id, label]) => ({ id, label }))
}

function kelvinRange(state) {
	const lights = lightsOf(state)
	if (!lights.length) return [1800, 10000]
	return [Math.min(...lights.map((l) => l.profile.kelvin_min)), Math.max(...lights.map((l) => l.profile.kelvin_max))]
}

function targets(options) {
	const picked = Array.isArray(options.lights) ? options.lights : [options.lights]
	return picked.includes(ALL) || !picked.length ? ALL : picked
}

// 0xRRGGBB -> hue 0-359, saturation 0-100 (the lights take HSI, not RGB)
function rgbToHueSat(color) {
	const r = ((color >> 16) & 0xff) / 255
	const g = ((color >> 8) & 0xff) / 255
	const b = (color & 0xff) / 255
	const max = Math.max(r, g, b)
	const delta = max - Math.min(r, g, b)
	let hue = 0
	if (delta > 0) {
		if (max === r) hue = ((g - b) / delta) % 6
		else if (max === g) hue = (b - r) / delta + 2
		else hue = (r - g) / delta + 4
		hue = Math.round(hue * 60)
		if (hue < 0) hue += 360
	}
	return { hue: hue % 360, saturation: max === 0 ? 0 : Math.round((delta / max) * 100) }
}

function buildActions(state, send) {
	const lights = { id: 'lights', type: 'multidropdown', label: 'Lights', default: [ALL], choices: lightChoices(state) }
	const [kMin, kMax] = kelvinRange(state)
	const set = (changes) => (action) => send({ targets: targets(action.options), ...changes(action.options) })
	const brightnessOptions = [
		{ id: 'withBrightness', type: 'checkbox', label: 'Also set brightness', default: false },
		{
			id: 'brightness',
			type: 'number',
			label: 'Brightness (%)',
			default: 50,
			min: 0,
			max: 100,
			step: 0.5,
			isVisible: (o) => !!o.withBrightness,
		},
	]
	const maybeBrightness = (o) => (o.withBrightness ? { brightness: Number(o.brightness) } : {})

	return {
		power: {
			name: 'Power',
			options: [
				lights,
				{
					id: 'state',
					type: 'dropdown',
					label: 'State',
					default: 'toggle',
					choices: [
						{ id: 'toggle', label: 'Toggle' },
						{ id: 'on', label: 'On' },
						{ id: 'off', label: 'Off' },
					],
				},
			],
			callback: set((o) => ({ power: o.state })),
		},
		brightness: {
			name: 'Set brightness',
			options: [
				lights,
				{ id: 'brightness', type: 'number', label: 'Brightness (%)', default: 50, min: 0, max: 100, step: 0.5 },
			],
			callback: set((o) => ({ brightness: Number(o.brightness) })),
		},
		brightness_adjust: {
			name: 'Adjust brightness',
			options: [
				lights,
				{ id: 'delta', type: 'number', label: 'Change (%)', default: 5, min: -100, max: 100, step: 0.5 },
			],
			callback: set((o) => ({ brightness_delta: Number(o.delta) })),
		},
		cct: {
			name: 'Set colour temperature',
			options: [
				lights,
				{ id: 'kelvin', type: 'number', label: 'Kelvin', default: 5600, min: kMin, max: kMax, step: 100 },
				{
					id: 'gm',
					type: 'number',
					label: 'Green (-) / magenta (+) tint',
					default: 0,
					min: -100,
					max: 100,
					tooltip: 'Only lights with tint control use this',
				},
				...brightnessOptions,
			],
			callback: set((o) => ({ mode: 'cct', kelvin: Number(o.kelvin), gm: Number(o.gm), ...maybeBrightness(o) })),
		},
		kelvin_adjust: {
			name: 'Adjust colour temperature',
			options: [
				lights,
				{ id: 'delta', type: 'number', label: 'Change (K)', default: 200, min: -5000, max: 5000, step: 100 },
			],
			callback: set((o) => ({ mode: 'cct', kelvin_delta: Number(o.delta) })),
		},
		color: {
			name: 'Set colour',
			description: 'RGB lights only; bi-colour lights ignore it',
			options: [lights, { id: 'color', type: 'colorpicker', label: 'Colour', default: rgb(255, 0, 0) }, ...brightnessOptions],
			callback: set((o) => ({ mode: 'hsi', ...rgbToHueSat(Number(o.color)), ...maybeBrightness(o) })),
		},
		hsi: {
			name: 'Set hue and saturation',
			description: 'RGB lights only; bi-colour lights ignore it',
			options: [
				lights,
				{ id: 'hue', type: 'number', label: 'Hue (degrees)', default: 0, min: 0, max: 359 },
				{ id: 'saturation', type: 'number', label: 'Saturation (%)', default: 100, min: 0, max: 100 },
				...brightnessOptions,
			],
			callback: set((o) => ({
				mode: 'hsi',
				hue: Number(o.hue),
				saturation: Number(o.saturation),
				...maybeBrightness(o),
			})),
		},
		hue_adjust: {
			name: 'Adjust hue',
			options: [lights, { id: 'delta', type: 'number', label: 'Change (degrees)', default: 10, min: -180, max: 180 }],
			callback: set((o) => ({ mode: 'hsi', hue_delta: Number(o.delta) })),
		},
		saturation_adjust: {
			name: 'Adjust saturation',
			options: [lights, { id: 'delta', type: 'number', label: 'Change (%)', default: 5, min: -100, max: 100 }],
			callback: set((o) => ({ mode: 'hsi', saturation_delta: Number(o.delta) })),
		},
		effect: {
			name: 'Start effect',
			description: 'Lights that lack the chosen effect keep doing what they were doing',
			options: [
				lights,
				{ id: 'effect', type: 'dropdown', label: 'Effect', default: 6, choices: effectChoices(state) },
				{ id: 'speed', type: 'number', label: 'Speed (0-100)', default: 50, min: 0, max: 100 },
				...brightnessOptions,
			],
			callback: set((o) => ({
				mode: 'effect',
				effect: Number(o.effect),
				speed: Number(o.speed),
				...maybeBrightness(o),
			})),
		},
		resync: {
			name: 'Re-send state to lights',
			description: 'Use after a light was changed on its own panel or power-cycled',
			options: [lights],
			callback: set(() => ({ resync: true })),
		},
	}
}

function buildFeedbacks(getState) {
	const one = () => ({
		id: 'light',
		type: 'dropdown',
		label: 'Light',
		default: (lightsOf(getState())[0] || {}).id || '',
		choices: lightsOf(getState()).map((l) => ({ id: l.id, label: l.name })),
	})
	const find = (id) => lightsOf(getState()).find((l) => l.id === id)
	return {
		light_on: {
			type: 'boolean',
			name: 'Light is on',
			defaultStyle: { bgcolor: AMBER, color: BLACK },
			options: [one()],
			callback: (fb) => !!(find(fb.options.light) || { state: {} }).state.on,
		},
		any_on: {
			type: 'boolean',
			name: 'Any light is on',
			defaultStyle: { bgcolor: AMBER, color: BLACK },
			options: [],
			callback: () => lightsOf(getState()).some((l) => l.state.on),
		},
		mode: {
			type: 'boolean',
			name: 'Light is in mode',
			defaultStyle: { bgcolor: rgb(0, 90, 160), color: WHITE },
			options: [
				one(),
				{
					id: 'mode',
					type: 'dropdown',
					label: 'Mode',
					default: 'cct',
					choices: [
						{ id: 'cct', label: 'Colour temperature' },
						{ id: 'hsi', label: 'Colour' },
						{ id: 'effect', label: 'Effect' },
					],
				},
			],
			callback: (fb) => (find(fb.options.light) || { state: {} }).state.mode === fb.options.mode,
		},
		disconnected: {
			type: 'boolean',
			name: 'Lights not reachable over Bluetooth',
			defaultStyle: { bgcolor: rgb(160, 0, 0), color: WHITE },
			options: [],
			callback: () => {
				const state = getState()
				return !(state && state.lights && state.lights.connected)
			},
		},
	}
}

function buildVariables(state) {
	const definitions = [{ variableId: 'bluetooth', name: 'Bluetooth link to the lights' }]
	const values = {
		bluetooth: state && state.lights ? (state.lights.connected ? 'Connected' : 'Disconnected') : 'No lights',
	}
	for (const light of lightsOf(state)) {
		const s = light.state
		const add = (key, name, value) => {
			definitions.push({ variableId: `${light.id}_${key}`, name: `${light.name}: ${name}` })
			values[`${light.id}_${key}`] = value
		}
		add('power', 'power', s.on ? 'On' : 'Off')
		add('brightness', 'brightness (%)', Math.round(s.brightness))
		add('kelvin', 'colour temperature (K)', s.kelvin)
		add('mode', 'mode', s.mode)
		add('hue', 'hue', s.hue)
		add('saturation', 'saturation (%)', s.saturation)
	}
	return { definitions, values }
}

function buildPresets(state, label) {
	const presets = {}
	const style = (text, extra) => ({ text, size: 'auto', color: WHITE, bgcolor: BLACK, ...extra })
	const press = (actionId, options) => ({ down: [{ actionId, options }], up: [] })
	const button = (id, category, name, text, steps, feedbacks, options) => {
		presets[id] = { type: 'button', category, name, style: style(text), steps, feedbacks: feedbacks || [], ...(options ? { options } : {}) }
	}
	const onStyle = { bgcolor: AMBER, color: BLACK }

	button('all_toggle', 'All lights', 'All on/off', 'ALL\\nLIGHTS', [press('power', { lights: [ALL], state: 'on' }), press('power', { lights: [ALL], state: 'off' })], [{ feedbackId: 'any_on', options: {}, style: onStyle }])
	button('all_on', 'All lights', 'All on', 'ALL\\nON', [press('power', { lights: [ALL], state: 'on' })])
	button('all_off', 'All lights', 'All off', 'ALL\\nOFF', [press('power', { lights: [ALL], state: 'off' })])
	button('all_up', 'All lights', 'All brighter', 'ALL\\n+5%', [press('brightness_adjust', { lights: [ALL], delta: 5 })])
	button('all_down', 'All lights', 'All dimmer', 'ALL\\n-5%', [press('brightness_adjust', { lights: [ALL], delta: -5 })])
	for (const kelvin of [3200, 4300, 5600]) {
		button(`all_${kelvin}`, 'All lights', `All ${kelvin} K`, `ALL\\n${kelvin}K`, [press('cct', { lights: [ALL], kelvin, gm: 0, withBrightness: false, brightness: 50 })])
	}
	button('bt_status', 'All lights', 'Bluetooth status', `LIGHTS\\n$(${label}:bluetooth)`, [press('resync', { lights: [ALL] })], [{ feedbackId: 'disconnected', options: {}, style: { bgcolor: rgb(160, 0, 0), color: WHITE } }])

	for (const light of lightsOf(state)) {
		const id = light.id
		const cat = light.name
		const v = (key) => `$(${label}:${id}_${key})`
		const lit = [{ feedbackId: 'light_on', options: { light: id }, style: onStyle }]
		button(`${id}_toggle`, cat, 'On/off', `${light.name}\\n${v('brightness')}%`, [press('power', { lights: [id], state: 'toggle' })], lit)
		button(`${id}_up`, cat, 'Brighter', `${id}\\n+5%`, [press('brightness_adjust', { lights: [id], delta: 5 })])
		button(`${id}_down`, cat, 'Dimmer', `${id}\\n-5%`, [press('brightness_adjust', { lights: [id], delta: -5 })])
		button(`${id}_warmer`, cat, 'Warmer', `${id}\\nWARMER`, [press('kelvin_adjust', { lights: [id], delta: -200 })])
		button(`${id}_cooler`, cat, 'Cooler', `${id}\\nCOOLER`, [press('kelvin_adjust', { lights: [id], delta: 200 })])
		// Stream Deck + dials: turn for brightness, push for on/off
		button(
			`${id}_dial_brightness`,
			cat,
			'Dial: brightness (push: on/off)',
			`${light.name}\\n${v('brightness')}%`,
			[
				{
					down: [{ actionId: 'power', options: { lights: [id], state: 'toggle' } }],
					up: [],
					rotate_left: [{ actionId: 'brightness_adjust', options: { lights: [id], delta: -2 } }],
					rotate_right: [{ actionId: 'brightness_adjust', options: { lights: [id], delta: 2 } }],
				},
			],
			lit,
			{ rotaryActions: true },
		)
		button(
			`${id}_dial_kelvin`,
			cat,
			'Dial: colour temperature',
			`${light.name}\\n${v('kelvin')}K`,
			[
				{
					down: [{ actionId: 'cct', options: { lights: [id], kelvin: 5600, gm: 0, withBrightness: false, brightness: 50 } }],
					up: [],
					rotate_left: [{ actionId: 'kelvin_adjust', options: { lights: [id], delta: -100 } }],
					rotate_right: [{ actionId: 'kelvin_adjust', options: { lights: [id], delta: 100 } }],
				},
			],
			[],
			{ rotaryActions: true },
		)
		if (light.profile.hsi) {
			button(
				`${id}_dial_hue`,
				cat,
				'Dial: hue',
				`${light.name}\\nHUE ${v('hue')}`,
				[
					{
						down: [],
						up: [],
						rotate_left: [{ actionId: 'hue_adjust', options: { lights: [id], delta: -5 } }],
						rotate_right: [{ actionId: 'hue_adjust', options: { lights: [id], delta: 5 } }],
					},
				],
				[{ feedbackId: 'mode', options: { light: id, mode: 'hsi' }, style: { bgcolor: rgb(0, 90, 160), color: WHITE } }],
				{ rotaryActions: true },
			)
		}
	}
	return presets
}

module.exports = { buildActions, buildFeedbacks, buildVariables, buildPresets, rgbToHueSat, lightsOf, DIM }
