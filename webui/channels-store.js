import { createStore } from "/js/AlpineStore.js";

export const store = createStore("channelsPlugin", {
    // Bus state
    running: false,
    adapters: {},
    activeConversations: 0,
    conversations: [],

    // UI state
    starting: false,
    stopping: false,
    message: null,
    pollTimer: null,
    lastRefreshed: null,

    init() {},

    async onOpen() {
        await this.refresh();
        this._startPolling();
    },

    cleanup() {
        this._stopPolling();
    },

    // ── Status ──

    async refresh() {
        try {
            const resp = await this._api("status", { action: "status" });
            this.running = resp.running || false;
            this.adapters = resp.adapters || {};
            this.activeConversations = resp.active_conversations || 0;

            if (this.running) {
                const convResp = await this._api("status", { action: "conversations" });
                this.conversations = convResp.conversations || [];
            }

            this.lastRefreshed = new Date().toLocaleTimeString();
        } catch (e) {
            console.error("[channels] status check failed:", e);
        }
    },

    // ── Controls ──

    async start() {
        this.starting = true;
        this.message = null;
        try {
            const resp = await this._api("status", { action: "start" });
            if (resp.ok) {
                this.running = true;
                this.message = { type: "success", text: "Channels bus started" };
                await this.refresh();
            } else {
                this.message = { type: "error", text: resp.error || "Failed to start" };
            }
        } catch (e) {
            this.message = { type: "error", text: e.message };
        }
        this.starting = false;
    },

    async stop() {
        if (!confirm("Stop all channel adapters? Active conversations will be dropped.")) return;
        this.stopping = true;
        this.message = null;
        try {
            const resp = await this._api("status", { action: "stop" });
            if (resp.ok) {
                this.running = false;
                this.adapters = {};
                this.activeConversations = 0;
                this.conversations = [];
                this.message = { type: "info", text: "Channels stopped" };
            }
        } catch (e) {
            this.message = { type: "error", text: e.message };
        }
        this.stopping = false;
    },

    // ── Computed ──

    get adapterList() {
        return Object.entries(this.adapters).map(([name, info]) => ({ name, ...info }));
    },

    get connectedCount() {
        return this.adapterList.filter(a => a.connected).length;
    },

    botLabel(adapter) {
        const info = adapter.bot_info || {};
        if (adapter.name === "telegram") {
            return info.username ? `@${info.username}` : "";
        }
        if (adapter.name === "discord") {
            const parts = [];
            if (info.username) parts.push(info.username);
            if (adapter.guild_count != null) parts.push(`${adapter.guild_count} guild(s)`);
            return parts.join(" — ");
        }
        if (adapter.name === "whatsapp") {
            const parts = [];
            if (info.verified_name) parts.push(info.verified_name);
            if (info.phone_number) parts.push(info.phone_number);
            return parts.join(" — ") || "";
        }
        return "";
    },

    // ── Polling ──

    _startPolling() {
        this._stopPolling();
        this.pollTimer = setInterval(() => this.refresh(), 5000);
    },

    _stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    },

    // ── Helpers ──

    async _api(endpoint, body) {
        const { callJsonApi } = await import("/js/api.js");
        return await callJsonApi(`plugins/channels_provider/${endpoint}`, body);
    },
});
