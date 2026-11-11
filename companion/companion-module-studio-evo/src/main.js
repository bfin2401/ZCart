const { InstanceBase, InstanceStatus, Regex, runEntrypoint } = require('@companion-module/base')
const { BridgeClient } = require('./bridge.js')
const { buildActions, buildFeedbacks, buildVariables, buildPresets } = require('./definitions.js')

class EvoInstance extends InstanceBase {
	async init(config) {
		this.state = null
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
					'This module talks to the studio-bridge service on the computer the EVO 8 is plugged into. ' +
					'Leave the defaults when that is the machine running Companion.',
			},
			{ type: 'textinput', id: 'host', label: 'Bridge address', width: 8, default: '127.0.0.1' },
			{ type: 'textinput', id: 'port', label: 'Bridge port', width: 4, default: '8765', regex: Regex.PORT },
		]
	}

	async configUpdated(config) {
		this.config = config
		if (this.client) this.client.stop()
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
		const post = (path, body) =>
			this.client.post(path, body).catch((error) => this.log('error', `EVO: ${error.message}`))
		this.setActionDefinitions(buildActions(post))
		this.setFeedbackDefinitions(buildFeedbacks(() => this.state))
		this.setVariableDefinitions(buildVariables(this.state).definitions)
		this.setPresetDefinitions(buildPresets(this.label))
		this.client.start()
	}

	onState(state) {
		this.state = state
		const evo = state.evo
		if (!evo) {
			this.updateStatus(InstanceStatus.BadConfig, 'The EVO is disabled in the bridge config')
		} else if (!evo.available) {
			this.updateStatus(InstanceStatus.ConnectionFailure, evo.error || 'EVO 8 not connected')
		} else if (!evo.smartgain.configured) {
			this.updateStatus(InstanceStatus.UnknownWarning, 'Connected; SmartGain not set up in the bridge yet')
		} else {
			this.updateStatus(InstanceStatus.Ok)
		}
		this.setVariableValues(buildVariables(state).values)
		this.checkFeedbacks()
	}
}

runEntrypoint(EvoInstance, [])
