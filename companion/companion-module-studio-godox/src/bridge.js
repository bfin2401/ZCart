// Client for the studio-bridge HTTP API: follows /api/state with a long poll
// and posts commands. Shared, unchanged, by both studio-bridge modules.

class BridgeClient {
	constructor({ host, port, onState, onStatus, log }) {
		this.base = `http://${host || '127.0.0.1'}:${port || 8765}`
		this.onState = onState
		this.onStatus = onStatus
		this.log = log || (() => {})
		this.revision = null
		this.stopped = true
		this.abort = null
	}

	start() {
		if (!this.stopped) return
		this.stopped = false
		this.loop()
	}

	stop() {
		this.stopped = true
		if (this.abort) this.abort.abort()
	}

	async loop() {
		let failing = false
		while (!this.stopped) {
			try {
				const query = this.revision === null ? '' : `?since=${this.revision}&wait=20`
				this.abort = new AbortController()
				const timer = setTimeout(() => this.abort.abort(), 30000)
				let state
				try {
					const reply = await fetch(`${this.base}/api/state${query}`, { signal: this.abort.signal })
					if (!reply.ok) throw new Error(`HTTP ${reply.status}`)
					state = await reply.json()
				} finally {
					clearTimeout(timer)
				}
				if (this.stopped) return
				this.revision = state.revision
				failing = false
				this.onStatus(true)
				this.onState(state)
			} catch (error) {
				if (this.stopped) return
				if (!failing) this.log('warn', `studio-bridge not reachable at ${this.base}: ${error.message}`)
				failing = true
				this.revision = null
				this.onStatus(false, error.message)
				await new Promise((resolve) => setTimeout(resolve, 2000))
			}
		}
	}

	// Resolves to the reply; rejects with the bridge's own message on failure.
	async post(path, body) {
		const reply = await fetch(`${this.base}${path}`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(body),
			signal: AbortSignal.timeout(5000),
		})
		let data = {}
		try {
			data = await reply.json()
		} catch (error) {
			// not JSON: fall through to the status check
		}
		if (!reply.ok) throw new Error(data.error || `HTTP ${reply.status}`)
		return data
	}
}

module.exports = { BridgeClient }
