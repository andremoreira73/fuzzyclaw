/**
 * Message Board — Alpine.js boardPanel component + board store.
 * Must be loaded BEFORE Alpine.js initializes (no defer).
 */
document.addEventListener('alpine:init', () => {
  Alpine.store('board', { open: false });

  Alpine.data('boardPanel', () => ({
    currentRunId: null,
    filterMode: 'all',
    pendingCount: 0,
    messageInput: '',
    sendError: '',
    showAutocomplete: false,
    // Drag state
    isDragging: false,
    dragX: null,
    dragY: null,
    offsetX: 0,
    offsetY: 0,
    // Resize state
    isResizing: false,
    panelW: 400,
    panelH: 500,
    // Polling intervals
    _feedInterval: null,
    _runsInterval: null,
    _badgeInterval: null,

    init() {
      const pos = JSON.parse(localStorage.getItem('boardPanelPos') || 'null');
      if (pos) { this.dragX = pos.x; this.dragY = pos.y; }
      const size = JSON.parse(localStorage.getItem('boardPanelSize') || 'null');
      if (size) { this.panelW = size.w; this.panelH = size.h; }

      // Watch panel open/close to start/stop polling
      this.$watch('$store.board.open', (open) => {
        if (open) this.onPanelOpen();
        else this.onPanelClose();
      });

      // Poll badge for auto-open (runs always, even when panel is closed)
      this._badgeInterval = setInterval(() => this.checkPending(), 3000);
    },

    destroy() {
      this.onPanelClose();
      if (this._badgeInterval) clearInterval(this._badgeInterval);
    },

    checkPending() {
      fetch('/board/badge/')
        .then(r => r.text())
        .then(html => {
          const match = html.match(/(\d+)/);
          const count = match ? parseInt(match[1]) : 0;
          if (count > 0 && this.pendingCount === 0 && !this.$store.board.open) {
            this.$store.board.open = true;
          }
          this.pendingCount = count;
        })
        .catch(err => console.warn('Board badge fetch failed:', err));
    },

    // --- Panel lifecycle ---
    onPanelOpen() {
      this.loadRuns();
      this._runsInterval = setInterval(() => this.loadRuns(), 10000);
      if (this.currentRunId) this.startFeedPolling();
    },

    onPanelClose() {
      this.stopFeedPolling();
      if (this._runsInterval) { clearInterval(this._runsInterval); this._runsInterval = null; }
    },

    startFeedPolling() {
      this.stopFeedPolling();
      if (!this.currentRunId) return;
      this.loadFeed();
      this._feedInterval = setInterval(() => this.loadFeed(), 3000);
    },

    stopFeedPolling() {
      if (this._feedInterval) { clearInterval(this._feedInterval); this._feedInterval = null; }
    },

    // --- Data loading ---
    loadRuns() {
      fetch('/board/active-runs/')
        .then(r => r.json())
        .then(runs => {
          const select = document.getElementById('board-run-select');
          if (!select) return;
          const prevId = this.currentRunId;
          select.innerHTML = '';
          if (runs.length === 0) {
            select.innerHTML = '<option value="" disabled selected>No active runs</option>';
            this.currentRunId = null;
            this.stopFeedPolling();
            return;
          }
          runs.forEach(r => {
            const opt = document.createElement('option');
            opt.value = r.id;
            opt.textContent = 'Run #' + r.id + ': ' + r.title;
            select.appendChild(opt);
          });
          // Keep previous selection if still valid, else pick first
          const ids = runs.map(r => String(r.id));
          if (prevId && ids.includes(String(prevId))) {
            select.value = prevId;
          } else {
            select.selectedIndex = 0;
            this.currentRunId = select.value;
            this.startFeedPolling();
          }
        })
        .catch(err => console.warn('Board runs fetch failed:', err));
    },

    loadFeed() {
      if (!this.currentRunId) return;
      const url = '/runs/' + this.currentRunId + '/board/?filter=' + this.filterMode;
      htmx.ajax('GET', url, {target: '#board-feed', swap: 'innerHTML'}).then(() => {
        const feed = document.getElementById('board-feed');
        if (feed) feed.scrollTop = feed.scrollHeight;
      });
    },

    // --- Run selector ---
    onRunChange(e) {
      this.currentRunId = e.target.value;
      this.startFeedPolling();
    },

    prevRun() {
      const s = document.getElementById('board-run-select');
      if (!s || s.selectedIndex <= 0) return;
      s.selectedIndex--;
      this.currentRunId = s.value;
      this.startFeedPolling();
    },

    nextRun() {
      const s = document.getElementById('board-run-select');
      if (!s || s.selectedIndex >= s.options.length - 1) return;
      s.selectedIndex++;
      this.currentRunId = s.value;
      this.startFeedPolling();
    },

    // --- Drag & Resize ---
    get panelStyle() {
      const x = this.dragX ?? (window.innerWidth - 420);
      const y = this.dragY ?? (window.innerHeight * 0.35);
      return 'left:' + Math.max(0, x) + 'px;top:' + Math.max(0, y) + 'px;width:' + this.panelW + 'px;height:' + this.panelH + 'px;';
    },
    startDrag(e) {
      this.isDragging = true;
      const rect = e.target.closest('.fixed').getBoundingClientRect();
      this.offsetX = e.clientX - rect.left;
      this.offsetY = e.clientY - rect.top;
    },
    onDrag(e) {
      if (!this.isDragging) return;
      this.dragX = e.clientX - this.offsetX;
      this.dragY = e.clientY - this.offsetY;
    },
    stopDrag() {
      if (!this.isDragging) return;
      this.isDragging = false;
      localStorage.setItem('boardPanelPos', JSON.stringify({x: this.dragX, y: this.dragY}));
    },
    startResize(e) {
      this.isResizing = true;
      this.offsetX = e.clientX;
      this.offsetY = e.clientY;
    },
    onResize(e) {
      if (!this.isResizing) return;
      const dw = e.clientX - this.offsetX;
      const dh = e.clientY - this.offsetY;
      this.panelW = Math.max(320, this.panelW + dw);
      this.panelH = Math.max(300, this.panelH + dh);
      this.offsetX = e.clientX;
      this.offsetY = e.clientY;
    },
    stopResize() {
      if (!this.isResizing) return;
      this.isResizing = false;
      localStorage.setItem('boardPanelSize', JSON.stringify({w: this.panelW, h: this.panelH}));
    },

    // --- Messaging ---
    onInput() {
      const val = this.messageInput;
      if (val.startsWith('@') && !val.includes(' ') && this.currentRunId) {
        this.showAutocomplete = true;
        fetch('/runs/' + this.currentRunId + '/board/participants/')
          .then(r => r.text())
          .then(html => {
            const el = document.getElementById('board-autocomplete');
            if (el) el.innerHTML = html;
          });
      } else {
        this.showAutocomplete = false;
      }
    },

    insertMention(id) {
      this.messageInput = '@' + id + ' ';
      this.showAutocomplete = false;
      this.$nextTick(() => this.$el.querySelector('input[type="text"]')?.focus());
    },

    sendMessage() {
      const msg = this.messageInput.trim();
      if (!msg || !this.currentRunId) return;
      const csrf = document.querySelector('body')?.getAttribute('data-csrf')
                || (document.cookie.match(/csrftoken=([^;]+)/) || [])[1]
                || '';
      fetch('/runs/' + this.currentRunId + '/board/reply/', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrf},
        body: 'message=' + encodeURIComponent(msg),
      }).then(r => {
        if (r.ok) {
          this.messageInput = '';
          this.loadFeed();
          this.sendError = '';
        } else {
          r.text().then(body => {
            // Strip HTML tags — display as plain text to avoid injection
            const tmp = document.createElement('div');
            tmp.innerHTML = body;
            this.sendError = tmp.textContent || 'Failed to send message.';
            setTimeout(() => { this.sendError = ''; }, 5000);
          });
        }
      }).catch(() => {
        this.sendError = 'Network error. Please try again.';
        setTimeout(() => { this.sendError = ''; }, 5000);
      });
    },
  }));
});
