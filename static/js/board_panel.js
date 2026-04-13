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
    fuzzyThinking: false,
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
    _fuzzyStatusInterval: null,

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
      this._fuzzyStatusInterval = setInterval(() => this.pollFuzzyStatus(), 2000);
      this.pollFuzzyStatus();
      if (this.currentRunId) this.startFeedPolling();
    },

    onPanelClose() {
      this.stopFeedPolling();
      if (this._runsInterval) { clearInterval(this._runsInterval); this._runsInterval = null; }
      if (this._fuzzyStatusInterval) { clearInterval(this._fuzzyStatusInterval); this._fuzzyStatusInterval = null; }
    },

    pollFuzzyStatus() {
      fetch('/board/fuzzy/status/')
        .then(r => r.json())
        .then(data => {
          const was = this.fuzzyThinking;
          this.fuzzyThinking = data.status === 'thinking';
          // Update the selector label when status changes
          if (was !== this.fuzzyThinking) {
            const select = document.getElementById('board-run-select');
            if (select) {
              const opt = [...select.options].find(o => o.value === 'fuzzy');
              if (opt) opt.textContent = (this.fuzzyThinking ? '\u25CF ' : '') + 'Fuzzy Assistant';
            }
          }
        })
        .catch(() => { this.fuzzyThinking = false; });
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
            // 'fuzzy' is a sentinel string, not a run ID
            const label = r.id === 'fuzzy' ? r.title : 'Run #' + r.id + ': ' + r.title;
            opt.textContent = (r.id === 'fuzzy' && this.fuzzyThinking ? '\u25CF ' : '') + label;
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

    _boardUrl(suffix) {
      // Route to fuzzy-specific endpoints when fuzzy is selected
      if (this.currentRunId === 'fuzzy') return '/board/fuzzy/' + suffix;
      return '/runs/' + this.currentRunId + '/board/' + suffix;
    },

    loadFeed() {
      if (!this.currentRunId) return;
      const url = this._boardUrl('?filter=' + this.filterMode);
      const feed = document.getElementById('board-feed');
      // Only auto-scroll if the user is already near the bottom
      const wasNearBottom = feed && (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80);
      htmx.ajax('GET', url, {target: '#board-feed', swap: 'innerHTML'}).then(() => {
        if (feed && wasNearBottom) feed.scrollTop = feed.scrollHeight;
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
    _acIndex: -1,

    onInput() {
      const val = this.messageInput;
      if (val.startsWith('@') && !val.includes(' ') && this.currentRunId) {
        this.showAutocomplete = true;
        this._acIndex = -1;
        const query = val.slice(1).toLowerCase();
        fetch(this._boardUrl('participants/'))
          .then(r => r.text())
          .then(html => {
            const el = document.getElementById('board-autocomplete');
            if (!el) return;
            el.innerHTML = html;
            // Filter by typed prefix
            if (query) {
              el.querySelectorAll('button[data-id]').forEach(btn => {
                const id = btn.dataset.id.toLowerCase();
                if (!id.startsWith(query) && id !== 'all') btn.style.display = 'none';
              });
            }
          });
      } else {
        this.showAutocomplete = false;
      }
    },

    onAutocompleteKey(e) {
      if (!this.showAutocomplete) return;
      const btns = [...(document.querySelectorAll('#board-autocomplete button[data-id]') || [])].filter(b => b.style.display !== 'none');
      if (!btns.length) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); this._acIndex = Math.min(this._acIndex + 1, btns.length - 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); this._acIndex = Math.max(this._acIndex - 1, 0); }
      else if (e.key === 'Enter' && this._acIndex >= 0) { e.preventDefault(); this.insertMention(btns[this._acIndex].dataset.id); return; }
      else if (e.key === 'Tab') { e.preventDefault(); this.insertMention(btns[Math.max(0, this._acIndex)].dataset.id); return; }
      else return;
      btns.forEach((b, i) => b.classList.toggle('bg-indigo-50', i === this._acIndex));
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
      fetch(this._boardUrl('reply/'), {
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
