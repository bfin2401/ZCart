const { InstanceBase, InstanceStatus, Regex, runEntrypoint } = require('@companion-module/base')
const { BridgeClient } = require('./bridge.js')
const { buildActions, buildFeedbacks, buildVariables, buildPresets, lightsOf } = require('./definitions.js')

class GodoxInstance extends InstanceBase {
	async init(config) {
		this.state = null
		this.shape = null
		await this.configUpdated(config)
	}

	async destroy() {
		if (this.client) this.client.stop()
	}

	getConfigFields() {
		return [
			{
				type: 'static-text',
				id: 'info',
				width: 12,
				label: 'studio-bridge',
				value:
					'This module talks to the studio-bridge service, which holds the Bluetooth link to the Godox lights. ' +
					'It normally runs on the same machine as Companion.',
			},
			{ type: 'textinput', id: 'host', label: 'Bridge address', width: 8, default: '127.0.0.1' },
			{ type: 'textinput', id: 'port', label: 'Bridge port', width: 4, default: '8765', regex: Regex.PORT },
		]
	}

	async configUpdated(config) {
		this.config = config
		if (this.client) this.client.stop()
		this.shape = null
		this.updateStatus(InstanceStatus.Connecting)
		this.client = new BridgeClient({
			host: config.host,
			port: config.port,
			log: (level, message) => this.log(level, message),
			onStatus: (ok, message) => {
				if (!ok) this.updateStatus(InstanceStatus.ConnectionFailure, `studio-bridge: ${message}`)
			},
			onState: (state) => this.onState(state),
		})
		this.define()
		this.client.start()
	}

	onState(state) {
		this.state = state
		const lights = state.lights
		if (!lights || !lights.lights.length) {
			this.updateStatus(InstanceStatus.BadConfig, "No lights in the bridge yet: run 'studio-bridge lights add'")
		} else if (!lights.connected) {
			this.updateStatus(InstanceStatus.ConnectionFailure, lights.error || 'Lights not reachable over Bluetooth')
		} else {
			this.updateStatus(InstanceStatus.Ok)
		}
		// Definitions depend on which lights exist, not on what they are doing.
		const shape = JSON.stringify(lightsOf(state).map((l) => [l.id, l.name, l.model]))
		if (shape !== this.shape) {
			this.shape = shape
			this.define()
		}
		this.setVariableValues(buildVariables(state).values)
		this.checkFeedbacks()
	}

	define() {
		const send = (body) =>
			this.client.post('/api/lights/set', body).catch((error) => this.log('error', `lights: ${error.message}`))
		this.setActionDefinitions(buildActions(this.state, send))
		this.setFeedbackDefinitions(buildFeedbacks(() => this.state))
		this.setVariableDefinitions(buildVariables(this.state).definitions)
		this.setPresetDefinitions(buildPresets(this.state, this.label))
	}
}

runEntrypoint(GodoxInstance, [])
