// Actions, feedbacks, variables and presets for the Audient EVO 8. Pure
// functions of the bridge state, so they can be tested without Companion.

const rgb = (r, g, b) => ((r & 0xff) << 16) | ((g & 0xff) << 8) | (b & 0xff)
const WHITE = rgb(255, 255, 255)
const BLACK = rgb(0, 0, 0)
const GREEN = rgb(0, 190, 80)
const RED = rgb(200, 0, 0)
const YELLOW = rgb(255, 200, 0)

const CHANNELS = [1, 2, 3, 4]
const channelChoices = CHANNELS.map((c) => ({ id: c, label: `Input ${c}` }))
const channel = { id: 'channel', type: 'dropdown', label: 'Input', default: 1, choices: channelChoices }
const onOffToggle = {
	id: 'state',
	type: 'dropdown',
	label: 'State',
	default: 'toggle',
	choices: [
		{ id: 'toggle', label: 'Toggle' },
		{ id: 'on', label: 'On' },
		{ id: 'off', label: 'Off' },
	],
}

const evoOf = (state) => (state && state.evo) || null
const inputOf = (state, number) => ((evoOf(state) || {}).inputs || []).find((i) => i.channel === Number(number)) || {}

// One input's last outcome; for "any input" (0) a failure anywhere wins.
function resultOf(results, channel) {
	results = results || {}
	if (Number(channel) !== 0) return results[String(Number(channel))] || null
	const all = Object.values(results)
	return all.includes('failed') ? 'failed' : all.includes('ok') ? 'ok' : null
}

function buildActions(post) {
	return {
		smartgain_start: {
			name: 'SmartGain: start',
			description: 'Starts SmartGain listening on the chosen inputs, as the green button on the EVO does',
			options: [
				{ id: 'channels', type: 'multidropdown', label: 'Inputs', default: [1], choices: channelChoices, minSelection: 1 },
			],
			callback: (action) =>
				post('/api/evo/smartgain', { action: 'start', channels: [].concat(action.options.channels).map(Number) }),
		},
		smartgain_clear: {
			name: 'SmartGain: clear result',
			description: 'Forgets the last outcome, so the button goes back to its plain colour',
			options: [
				{ id: 'channels', type: 'multidropdown', label: 'Inputs', default: CHANNELS, choices: channelChoices, minSelection: 1 },
			],
			callback: (action) =>
				post('/api/evo/smartgain', { action: 'clear', channels: [].concat(action.options.channels).map(Number) }),
		},
		smartgain_cancel: {
			name: 'SmartGain: cancel',
			options: [],
			callback: () => post('/api/evo/smartgain', { action: 'cancel' }),
		},
		phantom: {
			name: '48V phantom power',
			options: [channel, onOffToggle],
			callback: (action) =>
				post('/api/evo/phantom', { channel: Number(action.options.channel), state: action.options.state }),
		},
		mute: {
			name: 'Input mute',
			options: [channel, onOffToggle],
			callback: (action) =>
				post('/api/evo/mute', { channel: Number(action.options.channel), state: action.options.state }),
		},
		gain: {
			name: 'Set preamp gain',
			options: [channel, { id: 'db', type: 'number', label: 'Gain (dB)', default: 30, min: -8, max: 58 }],
			callback: (action) =>
				post('/api/evo/gain', { channel: Number(action.options.channel), db: Number(action.options.db) }),
		},
		gain_adjust: {
			name: 'Adjust preamp gain',
			options: [channel, { id: 'delta', type: 'number', label: 'Change (dB)', default: 1, min: -58, max: 58 }],
			callback: (action) =>
				post('/api/evo/gain', { channel: Number(action.options.channel), delta: Number(action.options.delta) }),
		},
	}
}

function buildFeedbacks(getState) {
	const smartgain = () => (evoOf(getState()) || {}).smartgain || {}
	return {
		smartgain_running: {
			type: 'boolean',
			name: 'SmartGain is running',
			defaultStyle: { bgcolor: RED, color: WHITE },
			options: [
				{
					id: 'channel',
					type: 'dropdown',
					label: 'Input',
					default: 0,
					choices: [{ id: 0, label: 'Any input' }, ...channelChoices],
				},
			],
			callback: (fb) => {
				const sg = smartgain()
				const wanted = Number(fb.options.channel)
				return !!sg.running && (wanted === 0 || (sg.channels || []).includes(wanted))
			},
		},
		smartgain_result: {
			type: 'boolean',
			name: 'SmartGain last result',
			description: 'Stays until the next run on that input, or until cleared',
			defaultStyle: { bgcolor: GREEN, color: BLACK },
			options: [
				{
					id: 'channel',
					type: 'dropdown',
					label: 'Input',
					default: 0,
					choices: [{ id: 0, label: 'Any input' }, ...channelChoices],
				},
				{
					id: 'result',
					type: 'dropdown',
					label: 'Result',
					default: 'ok',
					choices: [
						{ id: 'ok', label: 'Succeeded' },
						{ id: 'failed', label: 'Failed' },
					],
				},
			],
			callback: (fb) => resultOf(smartgain().results, fb.options.channel) === fb.options.result,
		},
		smartgain_unconfigured: {
			type: 'boolean',
			name: 'SmartGain switched off in the bridge',
			defaultStyle: { bgcolor: rgb(90, 90, 90), color: WHITE },
			options: [],
			callback: () => !smartgain().configured,
		},
		phantom: {
			type: 'boolean',
			name: '48V is on',
			defaultStyle: { bgcolor: RED, color: WHITE },
			options: [channel],
			callback: (fb) => inputOf(getState(), fb.options.channel).phantom === true,
		},
		mute: {
			type: 'boolean',
			name: 'Input is muted',
			defaultStyle: { bgcolor: RED, color: WHITE },
			options: [channel],
			callback: (fb) => inputOf(getState(), fb.options.channel).mute === true,
		},
		unavailable: {
			type: 'boolean',
			name: 'EVO not connected',
			defaultStyle: { bgcolor: rgb(160, 0, 0), color: WHITE },
			options: [],
			callback: () => !(evoOf(getState()) || {}).available,
		},
	}
}

function buildVariables(state) {
	const evo = evoOf(state) || {}
	const sg = evo.smartgain || {}
	const definitions = [
		{ variableId: 'status', name: 'EVO connection' },
		{ variableId: 'smartgain', name: 'SmartGain state' },
	]
	const values = {
		status: evo.available ? 'Connected' : 'Disconnected',
		smartgain: !sg.configured ? 'Off' : sg.running ? 'Listening' : 'Idle',
	}
	const text = (value, on, off) => (value === true ? on : value === false ? off : '?')
	for (const number of CHANNELS) {
		const input = inputOf(state, number)
		definitions.push(
			{ variableId: `gain_${number}`, name: `Input ${number} gain (dB)` },
			{ variableId: `phantom_${number}`, name: `Input ${number} 48V` },
			{ variableId: `mute_${number}`, name: `Input ${number} mute` },
			{ variableId: `smartgain_${number}`, name: `Input ${number} SmartGain result` },
		)
		const listening = sg.running && (sg.channels || []).includes(number)
		const result = resultOf(sg.results, number)
		values[`smartgain_${number}`] = listening ? 'Listening' : result === 'ok' ? 'OK' : result === 'failed' ? 'Failed' : '-'
		values[`gain_${number}`] = typeof input.gain_db === 'number' ? Math.round(input.gain_db) : '?'
		values[`phantom_${number}`] = text(input.phantom, 'On', 'Off')
		values[`mute_${number}`] = text(input.mute, 'Muted', 'Live')
	}
	return { definitions, values }
}

function buildPresets(label) {
	const presets = {}
	const button = (id, category, name, text, steps, feedbacks, options) => {
		presets[id] = {
			type: 'button',
			category,
			name,
			style: { text, size: 'auto', color: WHITE, bgcolor: BLACK },
			steps,
			feedbacks: feedbacks || [],
			...(options ? { options } : {}),
		}
	}
	const press = (actionId, options) => ({ down: [{ actionId, options }], up: [] })
	const unconfigured = { feedbackId: 'smartgain_unconfigured', options: {}, style: { bgcolor: rgb(90, 90, 90), color: WHITE } }
	const running = (ch) => ({ feedbackId: 'smartgain_running', options: { channel: ch }, style: { bgcolor: RED, color: WHITE } })
	const ok = (ch) => ({ feedbackId: 'smartgain_result', options: { channel: ch, result: 'ok' }, style: { bgcolor: GREEN, color: BLACK } })
	const failed = (ch) => ({ feedbackId: 'smartgain_result', options: { channel: ch, result: 'failed' }, style: { bgcolor: YELLOW, color: BLACK } })
	// Later entries win: plain, then the last result, then red while listening.
	const lamp = (ch) => [unconfigured, ok(ch), failed(ch), running(ch)]

	for (const ch of CHANNELS) {
		button(`smartgain_${ch}`, 'SmartGain', `SmartGain input ${ch}`, `SMART\\nGAIN ${ch}`, [press('smartgain_start', { channels: [ch] })], lamp(ch))
	}
	button('smartgain_12', 'SmartGain', 'SmartGain inputs 1+2', 'SMART\\nGAIN 1+2', [press('smartgain_start', { channels: [1, 2] })], lamp(0))
	button('smartgain_all', 'SmartGain', 'SmartGain all inputs', 'SMART\\nGAIN ALL', [press('smartgain_start', { channels: CHANNELS })], lamp(0))
	button('smartgain_cancel', 'SmartGain', 'Cancel SmartGain', 'CANCEL\\nSMARTGAIN', [press('smartgain_cancel', {})], [running(0)])
	button('smartgain_clear', 'SmartGain', 'Clear SmartGain results', 'CLEAR\\nRESULTS', [press('smartgain_clear', { channels: CHANNELS })])
	button('status', 'SmartGain', 'EVO status', `EVO\\n$(${label}:status)\\n$(${label}:smartgain)`, [], [{ feedbackId: 'unavailable', options: {}, style: { bgcolor: rgb(160, 0, 0), color: WHITE } }])

	for (const ch of CHANNELS) {
		const cat = `Input ${ch}`
		button(`phantom_${ch}`, cat, '48V', `48V ${ch}\\n$(${label}:phantom_${ch})`, [press('phantom', { channel: ch, state: 'toggle' })], [{ feedbackId: 'phantom', options: { channel: ch }, style: { bgcolor: RED, color: WHITE } }])
		button(`mute_${ch}`, cat, 'Mute', `IN ${ch}\\n$(${label}:mute_${ch})`, [press('mute', { channel: ch, state: 'toggle' })], [{ feedbackId: 'mute', options: { channel: ch }, style: { bgcolor: RED, color: WHITE } }])
		// For decks without dials: the + button shows the gain, so a column of
		// SmartGain / + / - reads top to bottom.
		button(`gain_up_${ch}`, cat, 'Gain +1 dB', `IN ${ch}  +\\n$(${label}:gain_${ch}) dB`, [press('gain_adjust', { channel: ch, delta: 1 })], [running(ch)])
		button(`gain_down_${ch}`, cat, 'Gain -1 dB', `IN ${ch}  -`, [press('gain_adjust', { channel: ch, delta: -1 })], [running(ch)])
		button(
			`gain_dial_${ch}`,
			cat,
			'Dial: gain (push: SmartGain)',
			`GAIN ${ch}\\n$(${label}:gain_${ch}) dB`,
			[
				{
					down: [{ actionId: 'smartgain_start', options: { channels: [ch] } }],
					up: [],
					rotate_left: [{ actionId: 'gain_adjust', options: { channel: ch, delta: -1 } }],
					rotate_right: [{ actionId: 'gain_adjust', options: { channel: ch, delta: 1 } }],
				},
			],
			[ok(ch), failed(ch), running(ch)],
			{ rotaryActions: true },
		)
	}
	return presets
}

module.exports = { buildActions, buildFeedbacks, buildVariables, buildPresets }
