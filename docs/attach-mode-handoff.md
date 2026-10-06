# Attach mode ("ใช้ Chrome ที่เปิดอยู่") - work-in-progress handoff

Status (2026-10-07): research is done (below). Implementation was STARTED and then stopped midway when the session moved to the cloud.
The partial code is a DRAFT: unfinished and untested. It is in:
- new files: pcm/attach.py, tests/test_attach.py
- edits to pcm/session.py, pcm/cdp.py, pcm/config.py, poomcatomanga.py, tests/snap_ui.py

Requirements:
- Settings.browser_mode = "own" (default, unchanged) | "attach".
- Attach to the user's running Chrome 144+ after they tick chrome://inspect/#remote-debugging.
  - Chrome writes DevToolsActivePort (port + /devtools/browser/<id>) in its user-data dir.
  - It asks Allow on every connect, and shows the automation banner while connected.
- Translate the front tab, or open the typed URL in a new tab. Follow its navigations and the tabs it opens; leave unrelated tabs alone.
- NEVER close or kill the user's Chrome, tabs or profile. Detach cleanly on Stop, on deny, on timeout and on app exit.
- A Thai guide when attach is not enabled, with a button that opens chrome://inspect/#remote-debugging.
- tests/test_attach.py drives a separate test Chrome (--remote-debugging-port=0, temp profile) through PCM_ATTACH_DIR.
- Then option B: a Chrome MV3 extension talking to the local app.

Final verification must happen on the user's Windows PC: the real Chrome 154 Allow flow and the Tk UI.

## Protocol research (verified on a separate test Chrome)

### Findings

VERIFIED BY EXPERIMENT. I used Chrome 154.0.8037.98 instances that I started myself, each with its own fresh --user-data-dir under scratchpad\attach. Each one was killed by its PID tree, and I removed the profiles afterwards. The scripts are kept in scratchpad\attach as e1_classic.py, e2_usermode.py, e3_usermode_cdp.py, e4_dialog.py, e5_dialog_input.py, e6_opener.py and e7_detach.py. I never touched the user's Chrome or %LOCALAPPDATA%\Google\Chrome\User Data.

1. **DevToolsActivePort format (E1, classic --remote-debugging-port=0).** The file has two lines and no trailing newline, for example '56723\n/devtools/browser/<uuid>'. It stays on disk while Chrome runs.

2. **Approval mode reproduced in a test profile.** Searching chrome.dll found these strings: the pref devtools.remote_debugging.user-enabled, the policy devtools.remote_debugging.allowed / RemoteDebuggingAllowed, and "chrome://inspect#remote-debugging". I pre-seeded Local State with {"devtools":{"remote_debugging":{"user-enabled":true}}}. Chrome then wrote DevToolsActivePort on a random port (61609) without being given --remote-debugging-port. What a client sees in this mode:
   - GET /json/version, /json/list and /json all return 404 (E2).
   - A websocket to /devtools/page/<REAL target id> gets HTTP 403 (E3). A fake id also gets 403 (E2).
   - A websocket to /devtools/browser/<any id> is accepted. Even a wrong id worked, so the id is not checked in this mode.
   - The handshake is held while a top-level window titled 'อนุญาตให้ใช้การแก้ไขข้อบกพร่องจากระยะไกลใช่ไหม' is visible. The handshake completed right after that window closed (E4/E5: window present at t=0.25–1.05 s, done at 1.3–1.6 s).
   - This matches a copy of Chromium v154 source that a parallel agent left in scratchpad\attach (v154_devtools_http_handler.cc, v154_devtools_connection_dialog.cc, v154_chrome_devtools_manager_delegate.cc):
     - In approval mode only the /devtools/browser prefix goes to the permission prompt. Everything else gets 403. A denial also returns 403 "Connection rejected".
     - The dialog activates the last active browser window. Its buttons are Allow, Cancel and Disable. Disable opens chrome://inspect#remote-debugging and denies.
     - Cancel has the initial focus and there is no default button. Closing the dialog counts as deny.
     - Chrome has no timeout of its own, so the client must time out.
     - The "controlled by automated test software" infobar is closed when the count of active connections drops to 0.
   - Caveat: in all 5 approval-mode connections the dialog closed with Allow within 0.8–1.9 s, and I never clicked it. GetLastInputInfo showed user input during each dialog, so I cannot say who accepted. The histogram DevTools.RemoteDebugging.ConnectionPermission recorded bucket 0 (kAllowed). These test dialogs appeared on the user's desktop.
   - Over the one browser websocket, flattened sessions (Target.attachToTarget flatten=true plus sessionId) accepted every command the session uses (E3): Page.enable, Network.enable(maxTotalBufferSize), Page.setBypassCSP, Page.addScriptToEvaluateOnNewDocument (it ran on the next navigation), Page.navigate, Runtime.evaluate, Page.captureScreenshot and Network.getCookies. A second client can connect at the same time.

3. **chrome:// URLs cannot be opened from the command line.** `chrome.exe --user-data-dir=<running test profile> chrome://inspect/#remote-debugging` opened chrome://newtab/ instead. The same route with https://example.com/ worked (E1). So the guide must copy the URL to the clipboard rather than launch it.

4. **openerId behaviour (E6, the same in headless and headed).**
   - openerId IS set for `<a target=_blank>`, for `<a target=_blank rel=noopener>`, and for window.open(...,'noopener'), even though canAccessOpener is false.
   - openerId is NOT set for ctrl+click, middle-click, or Target.createTarget.
   - In the Target.targetCreated event the url is '' and openerId is already present; Target.getTargets later reports both.

5. **Which tab is in front (E6).**
   - Only the active tab in each window reports document.visibilityState == 'visible', headless and headed alike.
   - A tab made with Target.createTarget becomes active and visible.
   - document.hasFocus() is unreliable: it returned true for a hidden tab.
   - Browser.getWindowForTarget works and returns windowId and windowState.

6. **Detach behaviour (E7, plus E1).** Closing our socket without an explicit detach leaves Chrome running. Tabs we created with Target.createTarget survive. In-page state survives (window variables, page.js listeners, so Alt+T keeps working). A script added with addScriptToEvaluateOnNewDocument by the dead session no longer runs after a reload.

7. **Library defaults that matter.**
   - websockets.connect defaults to open_timeout=10 s (websockets/asyncio/client.py:279).
   - SeleniumBase Connection.aopen (cdp_driver/connection.py:232-243) passes no open_timeout, so it would give up on the Allow wait after 10 s. It does set max_size=2**28 (connection.py:28).
   - The plain websockets default max_size is 1 MiB, which is too small for image payloads.

SELENIUMBASE (4.55) FACTS
- cdp_util.start_async(host, port) cannot be used to attach:
  - It builds a Config, which makes a temp uc_ profile.
  - Browser.start (browser.py:600-733) needs HTTP /json/version (678-690), which returns 404 in approval mode.
  - It then opens one websocket per tab at ws://host:port/devtools/page/<id> (update_targets 872-893, TargetCreated 244-260). Those get 403.
- Config(user_data_dir=X) OVERWRITES X\Default\Preferences (cdp_driver/config.py:95-106).
- Browser.stop() (browser.py:925-1072):
  - It terminates or kills `_process` (972-1032).
  - It deletes user_data_dir with rmtree unless that dir counts as custom (1063-1072).
- The module registers atexit(deconstruct_browser) at browser.py:1344. deconstruct_browser (39-74) stops, and deletes the profile of, every Browser registered in Browser.start (676).

CODEBASE MAP (paths under ghostmanga5\)

pcm/session.py
- **Launch.** `_launch` (376-390):
  - With keep_browser_profile, the profile is local_data_dir()\chrome-profile (379). Otherwise it is tempfile.mkdtemp(prefix='pcm_profile_') stored in self._profile_tmp (381, 389).
  - Headless is set when env PCM_HEADLESS=='1' (382).
  - It calls cdp_util.start_async(user_data_dir=profile, headless=headless) with a 60 s wait_for (384, 390), and falls back to a temp profile on failure (385-390).
  - The app passes no window size, user agent, extension or download options. Browser.get's UA/downloads logic (browser.py:390-471) is never called.
- **Start order.** `_main` (319-373):
  - DETAIL 'เริ่มแปล' line (330-332), then LOG 'กำลังเปิด Chrome…' (335).
  - _launch (336), then _ensure_prepared(self.browser.main_tab) (337). This is the only "first tab" logic.
  - Workers, _poll_loop and translator.prepare are started (338-341).
  - main_tab is navigated to s.last_url, racing stop_event (342-348). Then the ready message (350) and the stop wait (351).
  - Workers start only after launch and prepare.
- **Places that close Chrome or delete profiles** (all must be skipped or replaced in attach mode):
  - _main finally (355-373): the closers (360-364) only close renderer, ocr, translator and http. self.browser.stop() (365-370) kills Chrome and may delete a profile. The `_profile_tmp` rmtree is at 371-372.
  - Session.__init__ registers atexit(self._cleanup_files) (234-236). `_cleanup_files` (238-241) deletes tmpdir and _profile_tmp.
  - _thread_main finally (305-317) deletes tmpdir and emits 'stopped'.
  - SeleniumBase's own global atexit (see above).
- **Tab tracking.**
  - TabState (156-161) holds origin = performance.timeOrigin, the page.js token, path and vh. self.tabs is created at 218.
  - _live_tabs (458-463) returns browser.update_targets() plus browser.tabs, which is every page target. _prune_tabs is at 465-477.
  - _active_tab (479-487) picks the first tab whose visibilityState is 'visible', preferring self._tab, with tabs[0] as fallback.
  - _poll_loop (494-528) ticks every 0.35 s and refreshes targets every 1.5 s. _browser_alive (452-456) uses _process.returncode and returns True when _process is None. After 15 polls with no tab it stops (511-516).
  - There is NO opener logic anywhere.
- **Navigation and chapters.** There are no CDP navigation events.
  - _poll_tab (530-593) reads href, title and timeOrigin. It re-injects page.js when window.__gm is missing (541-542).
  - A new document gets a new TabState (547-549). The chapter key is (tid, origin, token, _chapter_path) (551).
  - _Away (126-127, 698-703) keeps jobs around for bfcache.
- **Per-tab setup.** _ensure_prepared (392-416) is keyed by safe.socket_id (396, 416).
  - It adds a ResponseReceived handler via tab.add_handler (398-403).
  - It sends Page.enable, Network.enable (200 MB / 40 MB buffers), Page.setBypassCSP(True) and addScriptToEvaluateOnNewDocument(page_js) (404-411), then evaluates page_js (412-415).
- **Code that assumes the app owns the browser:**
  - main_tab and navigating it (337, 344)
  - setBypassCSP (406) and the large network buffers (405)
  - _browser_alive (452) and the "no tabs left" stop (514)
  - _get_bytes path D (766-780): Network.getCookies plus the browser UA are sent from Python with httpx
  - _screenshot (790-810): captureBeyondViewport and hideChip on the visible tab
  - the chip (637-657), whose last text stays in the page
  - _toggle (1038-1046), which uses self._tab
  - the CSP F5 hint (587-591) and the chrome-error hint (534-538)

pcm/cdp.py
- call (31-57) is tied to SeleniumBase sockets: tab.aopen (33), tab.websocket (34-36), Transaction (37-38) and tab.mapper (45, 57).
- It keeps a per-tab id counter `_gm_ids` starting at 1_000_000 (41-44). That would collide if several sessions shared one socket.
- socket_id is at 26-28. evaluate (60-72) is built on call.

pcm/page.js
- The guard runs once per top frame (line 6).
- The parts the session drives: scan (149-183) and apply (298-320), which keeps blob URLs in gm.done.
- toggle (335-346) and the Alt+T listener (363-365) work without Python.
- setStatus and hideChip are at 349-361.
- The canvas hook (29-39) and the MutationObserver (367-389) stay after detach. That is harmless.

pcm/typeset.py
- The Renderer (297-341) has its own headless Chrome, with mkdtemp('pcm_render_') and start_async(headless=True) (318-321).
- close() calls b.stop() and deletes the profile (330-341). The crash-restart path is at 360-367.
- It is independent of the session's browser.

pcm/config.py
- Settings is at 91-127. The browser section is 125-126 (keep_browser_profile=True).
- load (156-172) coerces each value by the type of its default and does no value validation. save is at 175-179. THEMES (88) is the pattern for option lists.

poomcatomanga.py
- Card 1 (507-514): title 'ลิงก์ตอนที่จะอ่าน', URL Entry 511-513, 'วางลิงก์' button 514.
- _set_state (623-637): the starting text 'กำลังเปิด Chrome…' is at 630.
- start (752-774): the URL is required at 756-758, normalized at 759-766, and the Session starts at 767-770. stop is at 776-779.
- _drain (794-827) handles event kinds log, progress, stopped and call. 'progress' switches starting to running (808-809).
- on_close (829-837) calls stop and join(8).
- _collect_settings is at 733-744.
- SettingsWindow._page_advanced (356-362) has the keep_browser_profile toggle at 360. The _toggle helper (237-242) returns only the variable. _apply is at 376-386.
- selftest (840-869) runs in own mode.

tests
- test_session.py:
  - PORT comes from PCM_TEST_PORT (26), where 0 means any free port.
  - serve() and site_url() are at 40-52.
  - main() is at 55-107 and builds Settings(keep_browser_profile=False).
  - It probes sess._tab via run_coroutine_threadsafe (91-99).
- test_ui.py: sets PCM_HEADLESS (13) and backs up and restores settings (25-27, 64-70).
- test_units.py: small_units (374-390) uses check() style.
- The test site has index.html, chapter_b.html, flip.html, and nav.html, which redirects to chapter_b after 2.5 s.

NOTE: another agent is using the same scratchpad\attach folder (profA, profB, uia.ps1, the Chromium sources). Its Chrome (profB, browser PID 15940) is still running. I left it alone.

### Risks

- Killing the user's Chrome: if SeleniumBase Browser.stop() (browser.py:925-1072) is ever called on an attach-mode browser, it can terminate a process. The SeleniumBase global atexit deconstruct_browser (browser.py:39-74, 1344) stops every registered Browser. Attach mode must not build any SeleniumBase Browser or call start_async with host/port. _main's finally (session.py:365-370) must branch to detach().
- Corrupting the user's profile: SeleniumBase Config(user_data_dir=X) OVERWRITES X\Default\Preferences (cdp_driver/config.py:95-106). Browser.stop rmtrees non-custom user data dirs (1063-1072). Never pass the real User Data dir to anything in SeleniumBase. Only read DevToolsActivePort, never write or delete it.
- Closing user tabs or windows: never send Browser.close, Target.closeTarget, Page.close, Browser.crash or Target.disposeBrowserContext. Add a hard deny-list in the attach connection. Never Page.navigate a pre-existing user tab; only a tab we created (Target.createTarget) for a supplied URL. E7 verified that tabs we create survive our disconnect.
- Own-mode cleanup leaking into attach mode: session.py atexit _cleanup_files (234-241) and the finally rmtree of _profile_tmp (371-372) are safe only while _profile_tmp stays None in attach mode. Do not call _launch in attach mode.
- Tests touching the real browser: if PCM_ATTACH_DIR is set, find_endpoint must use ONLY that dir and never fall back to %LOCALAPPDATA% browser dirs. Otherwise a test could attach to the user's real Chrome. Tests must kill only the PID of the Chrome they started.
- SeleniumBase per-tab transport does not work in approval mode: /devtools/page/<id> gives HTTP 403 and /json gives 404 (verified). Reusing Tab/Connection objects or start_async(host, port) will fail. A single browser websocket with flattened sessions is required. The per-tab id counter in cdp.call (cdp.py:41-44) would collide on a shared socket, so use one socket-wide counter.
- Allow-dialog timeout and leaks: websockets' default open_timeout of 10 s (and SeleniumBase aopen) would abort the Allow wait. Pass open_timeout=60 explicitly. If the client times out or the user presses Stop, Chrome keeps the dialog open (it has no timeout). A later Allow is harmless, but the user may see a stale prompt. Tell them to press ยกเลิก.
- Accidental denial: Cancel is focused first and there is no default button (Chromium source). The dialog takes focus from the last active Chrome window, so Enter or Space while typing denies. Tell users to click Allow with the mouse. In my runs the dialogs were accepted within 0.8–1.9 s with user input present, and I could not attribute who accepted. Do not assume auto-accept.
- Half-started session on denial or timeout: attach must finish before workers, the poll loop and translator.prepare start (session.py:338-341). The renderer is lazy, so no second Chrome launches. Stop pressed during the wait must cancel the connect by racing stop_event, as in 344-348. The 'stopped' event must always be emitted.
- Following the wrong tabs (privacy): AttachedBrowser.tabs must return only followed tabs. Otherwise _active_tab (479-487) and _poll_tab would inject page.js into Gmail etc. and send images to Google Lens. Front-tab picking may briefly attach to http(s) tabs to read visibilityState only; detach them at once and do not log their URLs.
- Followed tab navigates to an unrelated site: if the user types another site into the followed tab, the requirement ('follow navigations') means page.js is injected there too and big images are OCR'd. Consider pausing when the host changes, or at least document it.
- ctrl+click and middle-click give no openerId (verified), so next-chapter tabs opened that way are missed unless a 'new tab, same host as a followed tab' rule is added. That rule could pick up a second manga tab on the same site, which is acceptable.
- Picking the front tab: hasFocus() is unreliable. visibilityState shows one visible tab per window, so several windows are ambiguous. After enabling, the front tab is likely chrome://inspect, so filter to http/https/file. Tab switching between Start and selection is a race; re-check if the selected target disappears before attaching.
- Stale DevToolsActivePort: after Chrome exits, the port can be reused by another service (e.g. the 8765 server). A non-Chrome server answering 403 would be misreported as 'denied'. Treat connection refused and non-403 handshake failures as stale and show the guide.
- Effects on the user's page while attached: Network.enable with 200 MB buffers, setBypassCSP and the injected agent. All of it is per-session and released on detach (verified for addScriptToEvaluateOnNewDocument). But the chip text and blob URLs remain, so set a final chip text before detaching.
- _get_bytes path D (session.py:766-780) reads the user's real cookies for the image URL and replays them from Python with httpx. That is the same host the browser would send them to, but it now involves the user's real logins.
- _screenshot (790-810) uses captureBeyondViewport on the user's visible tab, which can briefly resize or flicker the page they are reading.
- The 'controlled by automated test software' infobar only goes away when our websocket closes. Detach must close the socket. On an abrupt app exit, the OS closes the TCP socket, so Chrome drops the client, but the infobar may linger until then.
- Large CDP messages: getResponseBody, __gm.grab data URLs and __gm.apply payloads can be tens of MB. The raw websocket needs max_size=2**28 (the websockets default is 1 MiB).
- Thread and loop safety: AttachedBrowser and its sockets must live only on the session event loop. The Tk thread must only signal stop via stop_event, as today. on_close joins for 8 s; detach must fit inside it (use short per-call timeouts).
- Edge, Brave and Chrome Beta support of the chrome://inspect toggle is unverified (I only verified Chrome 154 stable). The guide text must use the right scheme (edge://inspect for Edge) and say Chrome 144 or newer is needed.
- Shared scratchpad: a parallel agent works in the same scratchpad\attach folder with its own Chrome (profB). Cleanup scripts must not rmtree the whole folder or kill chrome.exe by name or by command-line pattern.

### Recommended design

Overview: own mode stays exactly as it is. Attach mode uses ONE raw CDP websocket to the browser endpoint read from DevToolsActivePort, with flattened sessions per followed tab. The rest of Session stays almost unchanged because the code is duck-typed through pcm/cdp.call.

1) pcm/config.py
- Next to THEMES (88), add BROWSER_MODES = [("own", "Chrome ของแอป"), ("attach", "ใช้ Chrome ที่เปิดอยู่")].
- In Settings' browser section (125-126), add `browser_mode: str = "own"` with a short comment.
- In load(), after the loop (172), add `if s.browser_mode not in ("own", "attach"): s.browser_mode = "own"`.
- Nothing else changes. save() and asdict handle the new field.

2) New pcm/attach.py (about 200 lines, no SeleniumBase Browser or Config)

a. Finding the endpoint
- BROWSERS is an ordered list of (name, path under %LOCALAPPDATA%):
  - Chrome: Google\Chrome\User Data
  - Chrome Beta: Google\Chrome Beta\User Data
  - Chrome Dev: Google\Chrome Dev\User Data
  - Chrome Canary: Google\Chrome SxS\User Data
  - Edge: Microsoft\Edge\User Data
  - Brave: BraveSoftware\Brave-Browser\User Data
- find_endpoint() returns (Endpoint(name, dir, port, path) | None, reason in 'missing' | 'stale').
  - If env PCM_ATTACH_DIR is set, search ONLY that dir.
  - Read DevToolsActivePort read-only. Line 1 must be an int port; line 2 must start with '/devtools/browser'.
  - Check liveness with socket.create_connection(("127.0.0.1", port), 0.5).
- ATTACH_WAIT = float(os.environ.get("PCM_ATTACH_TIMEOUT") or 60).
- AttachError(msg, kind), where kind is one of missing, stale, denied, timeout, notab, closed.

b. Conn (one browser socket)
- Open with websockets.connect(f"ws://127.0.0.1:{port}{path}", open_timeout=ATTACH_WAIT, max_size=2**28).
- Mapping errors:
  - InvalidStatus 403 → denied.
  - TimeoutError → timeout.
  - OSError, ConnectionRefused or any other status → stale.
- Use one socket-wide id counter.
- send(cmd, session_id=None, timeout) reuses seleniumbase connection.Transaction for mycdp request and response parsing, adds "sessionId" when given, and raises cdp.CDPError on error or timeout.
- A reader task routes replies by id. It routes events that have a sessionId to that SessionTab's handlers, via mycdp.util.parse_json_event; handlers are called (event, tab), as SeleniumBase's Listener does.
- Target.detachedFromTarget marks that session dead.
- `alive` means the socket is OPEN and the reader is running.
- DENY = {Browser.close, Browser.crash, Target.closeTarget, Page.close, Target.disposeBrowserContext}. Refuse these before sending.

c. SessionTab
- Fields: target_id, session_id, url, title, opener_id.
- `send_cdp(cmd, timeout)` sends on the shared Conn with this session.
- `add_handler(type, fn)` registers an event handler.
- `websocket` is a new object() per attach, so safe.socket_id changes on re-attach and _ensure_prepared runs again.

d. AttachedBrowser
- Fields: conn, endpoint, followed (dict tid → SessionTab), start_ids (target ids that existed at connect).
- open_tab(url): Target.createTarget(url). This opens a new tab in the last active window and brings it to the front (verified). Follow the new tab.
- pick_front():
  - Candidates are page targets with no subtype and an http, https or file URL. chrome://inspect and other chrome:// pages are skipped.
  - For each candidate: attach, evaluate only document.visibilityState, and detach the ones not chosen.
  - With exactly one visible tab, use it.
  - With several visible tabs (several windows), break the tie by Win32 z-order: EnumWindows from top to bottom over visible 'Chrome_WidgetWin_1' windows. Strip the ' - Google Chrome' / ' - Microsoft Edge' / ' - Brave' suffix and match the remaining title against the candidates. If nothing matches, take the first visible tab.
  - With none, raise AttachError('notab', 'ไม่พบแท็บมังงะที่เปิดอยู่ด้านหน้า — สลับไปที่แท็บตอนที่จะอ่านใน Chrome แล้วกดเริ่มอีกครั้ง (หรือวางลิงก์)').
- update_targets(): call Target.getTargets.
  - Drop followed tabs that are gone.
  - Follow a new page target when its opener_id is in followed, OR when it is not in start_ids and its URL host equals the host of a followed tab. The host rule is needed because ctrl+click and middle-click give no openerId (verified).
  - Attach lazily with flatten=True.
- `tabs` returns list(followed.values()), so unrelated tabs are never exposed to Session.
- detach():
  - For each followed tab, best effort with a 2 s timeout, evaluate `window.__gm && __gm.setStatus('PoomCatoManga: หยุดแปลแล้ว • Alt+T สลับต้นฉบับ')`.
  - Then send Target.detachFromTarget (2 s each), then ws.close().
  - Nothing else.

3) pcm/cdp.py
- At the top of call() (line 31), add: `send = getattr(tab, "send_cdp", None)`, then `if send is not None: return await send(cmd, timeout)`.
- evaluate() and socket_id() need no change. Own-mode behaviour is unchanged.

4) pcm/session.py
- __init__ (207-236): add `self.attach = self.s.browser_mode == "attach"`.
- _main (319-373):
  - Extend the DETAIL start line (330-332) with ` browser=own|attach`.
  - Replace lines 335-337 with an if/else.
    - attach:
      - LOG 'กำลังเชื่อมต่อ Chrome ที่เปิดอยู่…'.
      - Run `tab = await self._attach()`, raced against stop_event the same way as 344-348, so Stop cancels the Allow wait.
      - Then `DETAIL.info("ใช้ Chrome ที่เปิดอยู่: %s port %d แท็บ %s", ep.name, ep.port, url)`. Do not log the ws path or id.
    - own: the existing code, unchanged.
    - Then `await self._ensure_prepared(tab)`.
  - Workers and the poll loop must start after this point (338-341 already do). So a denial or timeout starts nothing: the renderer is lazy, and the closers are no-ops.
  - Lines 342-348:
    - own: unchanged.
    - attach with a URL: the tab was already created by open_tab. Only wait for it to be ready: move the readiness loop (442-450) out of _navigate into `_wait_ready(tab)`.
    - attach without a URL: no navigation; LOG 'แปลแท็บ: <title>'.
  - Before the generic except (352-354), add `except AttachError as e: LOG.error(e.msg); self._emit("attach_failed", e.kind)`.
  - finally (365-372): if self.attach, `await asyncio.wait_for(self.browser.detach(), 8)` inside try. Otherwise keep the existing browser.stop() block. Guard the _profile_tmp rmtree with `not self.attach`.
  - Add the attach-mode final log line 'หยุดแล้ว — Chrome ของคุณยังเปิดอยู่'.
- _attach():
  - Run find_endpoint(). On missing or stale, raise AttachError with a Thai guide message.
  - `self._emit("status", "กด Allow ในหน้าต่าง Chrome เพื่ออนุญาต")` and LOG the same text.
  - Connect. Denial gives 'Chrome ไม่อนุญาตให้เชื่อมต่อ'. Timeout gives 'รอกด Allow เกิน 60 วินาที — ถ้า Chrome ยังถามอยู่ให้กด ยกเลิก แล้วลองใหม่'.
  - Then `self.browser = AttachedBrowser(...)`, and pick_front() or open_tab(s.last_url).
- _browser_alive (452-456): add `if self.attach: return self.browser.alive`.
- _poll_loop messages for attach mode:
  - line 501: 'Chrome ตัดการเชื่อมต่อแล้ว — หยุดการแปล'
  - line 514: 'ปิดแท็บที่แปลอยู่แล้ว — หยุดการแปล'
- _launch, _live_tabs, _prune_tabs, _active_tab, _poll_tab, _get_bytes, _screenshot and _toggle: no code change, because AttachedBrowser provides update_targets() and tabs, and SessionTab provides add_handler and send_cdp.

5) poomcatomanga.py
- _build, card 1 (507-514):
  - Add `self.attach = tk.BooleanVar(value=s.browser_mode == "attach")`.
  - Row 1: a Checkbutton 'ใช้ Chrome ที่เปิดอยู่ (ไม่ต้องวางลิงก์)' with bootstyle 'success-round-toggle' and command=self._sync_mode.
  - Row 2: a hint label with bootstyle (INVERSE, SECONDARY), shown only in attach mode: 'เว้นลิงก์ว่าง = แปลแท็บที่เปิดอยู่ด้านหน้าใน Chrome • ใส่ลิงก์ = เปิดในแท็บใหม่', plus a link button 'วิธีเปิดใช้' that opens the guide.
- _collect_settings (733-744): add `s.browser_mode = 'attach' if self.attach.get() else 'own'`.
- start (752-774):
  - Require a URL only in own mode. Normalize only when the URL is non-empty.
  - In attach mode, run a pre-flight `find_endpoint()`. If it fails, call `show_attach_guide(reason)` and return while still idle.
- _set_state (623-637):
  - In attach mode the starting text is 'กำลังเชื่อมต่อ Chrome…'.
  - Disable the toggle unless the state is idle.
- _drain (794-827): handle two new event kinds.
  - ('status', text): `self.status.configure(text='●  ' + text)`.
  - ('attach_failed', kind): missing or stale → show_attach_guide. Other kinds → Messagebox.show_warning with the Thai text.
- show_attach_guide(reason): a transient ttk.Toplevel.
  - Steps:
    1. เปิด Chrome แล้ววาง chrome://inspect/#remote-debugging ในแถบที่อยู่
    2. ติ๊ก "Allow remote debugging for this browser instance"
    3. กลับไปที่แท็บมังงะแล้วกดเริ่ม
  - Notes: Chrome จะถามให้กด Allow ทุกครั้ง; ต้องใช้ Chrome 144+.
  - Buttons:
    - 'คัดลอกลิงก์': clipboard_clear/append, then show 'คัดลอกแล้ว — วางในแถบที่อยู่ของ Chrome'.
    - 'ลองอีกครั้ง': close the guide and run start().
    - 'ปิด'.
  - Do NOT launch chrome.exe with the chrome:// URL; it was verified to open New Tab instead. Use the edge:// wording when the endpoint is Edge.
- SettingsWindow._page_advanced (356-362): disable the keep_browser_profile toggle when app.attach is on, with the muted note 'ไม่มีผลเมื่อใช้ Chrome ที่เปิดอยู่'. Change _toggle (237-242) to also return the widget, or grid it locally.
- on_close and selftest: unchanged. selftest stays in own mode.

6) Tests
- test_units.small_units (374-390):
  - Browser_mode validation.
  - find_endpoint with PCM_ATTACH_DIR set to a temp dir in four cases: no file → missing; a closed port → stale; garbage → missing; a live listener → ok.
  - Check that PCM_ATTACH_DIR stops any fallback to the real browser dirs.
- New tests/test_attach.py, a plain script that reuses serve() and site_url() from test_session.py with PCM_TEST_PORT=0.
  - launch_user_chrome(urls):
    - Make a profile with tempfile.mkdtemp(prefix='pcm_test_user_').
    - Run Popen([chrome, --user-data-dir=p, --remote-debugging-port=0, --no-first-run, --no-default-browser-check, (+ --headless=new when PCM_HEADLESS=1), *urls]).
    - Wait for DevToolsActivePort, then set os.environ['PCM_ATTACH_DIR'] = p.
    - Classic mode writes the same file and accepts the same browser websocket and flattened sessions without a dialog.
  - kill_tree(pid) kills only that PID and its children (psutil). The profile is deleted with rmtree in finally.
  - Case A, front tab:
    - Open urls=[decoy http://localhost:P/flip.html, manga http://127.0.0.1:P/index.html]. The last URL becomes the active tab; headless visibility was verified.
    - Run with browser_mode='attach', last_url=''.
    - Wait for progress. From a separate raw CDP client, check that the decoy has no window.__gm and the manga images are data-gm-state=done.
    - After stop and join, assert:
      - proc.poll() is None
      - the set of page targets is unchanged
      - DevToolsActivePort still exists
      - sess._profile_tmp is None
      - there is no new pcm_profile_* dir
  - Case B, URL: last_url = chapter_b. A new tab is created and translated, and all 3 tabs are still open after stop.
  - Case C, opener: add tests/site/open.html, which opens chapter_b with window.open or target=_blank. Assert the new tab gets followed and translated.
  - Case D, failures (no Chrome needed):
    - Empty dir → missing.
    - Stale port → stale.
    - An asyncio server that accepts TCP but never answers, with PCM_ATTACH_TIMEOUT=3 → timeout in about 4 s.
    - A websockets server whose process_request returns 403 → denied.
    - In each case, check that the 'attach_failed' and 'stopped' events arrive, no worker ran, and sess.renderer._browser is None.
  - Case E: Stop during the hanging wait → 'stopped' within about 2 s.
- test_ui.py: when PCM_TEST_ATTACH=1, launch the test Chrome, call app.attach.set(True) and app.url.set(''), and at the end assert the Chrome PID is still alive before killing it. Settings backup and restore already exist (25-27, 64-70).
- Approval mode with the real Allow dialog can only be tested by hand: seed Local State with {"devtools":{"remote_debugging":{"user-enabled":true}}} in a temp profile, with no --remote-debugging-port. Keep this out of automated runs.

## Codebase map / change plan

### Findings

ALL EXPERIMENTS USED MY OWN Chrome/Edge instances (fresh --user-data-dir under ...\scratchpad\attach\prof{A,B,C,D,E}), killed by PID only. User's chrome.exe 9024/7544 and msedge.exe 19708/17060 confirmed still running at the end. Chrome under test: 154.0.8037.98. Chromium sources were fetched at tag 154.0.8037.98 (scratch files v154_*.cc).

!! TWO THINGS THE USER SHOULD BE TOLD
(1) Early on I ran `"C:\Program Files\Google\Chrome\Application\chrome.exe" --version` WITHOUT --user-data-dir. On Windows that does not print a version. It handed the command to the user's running Chrome (it printed the garbled Thai form of "Opening in existing browser session"), so a new empty window or tab may have opened in their real Chrome. Nothing was closed or connected. They may want to close it.
(2) In my approval-mode test Chrome (profB), someone at the machine clicked "Allow" on my test permission dialogs (connections opened after 2.5s, 1.3s, 12.6s and 14.9s). My UIA InvokePattern clicks on the dialog buttons had NO effect (Invoke returned at 0.18s and the handshake still stayed pending). So I could not drive Allow or Cancel myself. The Cancel/deny behaviour below comes from the source code, not from an experiment.

=== 1. Connecting: protocol facts (VERIFIED) ===
- DevToolsActivePort format: two lines, no trailing newline. Observed bytes: b'52127\n/devtools/browser/57a569c7-...'. The writer is devtools_http_handler.cc ("%d\n%s"). It is the same in classic mode and approval mode.
- Approval mode (the chrome://inspect toggle):
  - Turned on by the Local State pref "devtools.remote_debugging.user-enabled"=true. The policy pref "devtools.remote_debugging.allowed" must also be true (remote_debugging_server.cc:185-190, 216-283, 397-416). I reproduced it by pre-seeding {"devtools":{"remote_debugging":{"user-enabled":true}}} in a fresh profile's Local State and launching without --remote-debugging-port. DevToolsActivePort was written at once.
  - It works with the DEFAULT user-data-dir. The default-dir check only applies to --remote-debugging-port/pipe, which also take precedence over approval mode.
  - HTTP is gone: GET /json/version, /json/list, /json and / all returned HTTP 404 (verified). Source: OnJsonRequest, OnDiscoveryPageRequest and OnFrontendResourceRequest all Send404 when kWithApprovalOnly.
  - Only the browser socket is accepted: ws://127.0.0.1:<port>/devtools/page/<id> gave HTTP 403 immediately with no dialog (verified). Source devtools_http_handler.cc:858-870: only paths starting with "/devtools/browser" go to the dialog, everything else gets Send403 "Connection rejected". The GUID is not required: "/devtools/browser" alone also produced the dialog (verified).
- The handshake BLOCKS until the user answers.
  - Verified: the dialog was visible through UIA 0.16s after connect, and the socket stayed in the opening handshake. With open_timeout=20 the client got `TimeoutError: timed out during opening handshake` at 20.05s. On Allow the socket opens (101).
  - Deny, close (X/Esc), "Turn off in settings" (opens chrome://inspect#remote-debugging), or no browser window at all → HTTP 403 "Connection rejected" (source: HandleDebuggingApproval and devtools_connection_dialog.cc). The websockets library raises InvalidStatus (403), as verified on the page path, which uses the same Send403.
- EVERY websocket connection gets its own dialog (seen in all 6 watched attempts). The dialog:
  - is browser-modal in GlobalBrowserCollection::GetLastActiveBrowser() and calls Activate() on that window;
  - has the title "Allow remote debugging?" and the buttons "Turn off in settings" / "Allow" / "Cancel";
  - focuses Cancel first and has no default button;
  - has no timeout.
- The dialog OUTLIVES a client that gave up (verified: still on screen after my 20s timeout). Later someone clicked Allow on that stale dialog. Result: the "Chrome is being controlled by automated test software" bar appeared in BOTH windows with no client connected, and it stayed (verified by UIA). After a normal Allow, a clean close removes the bar (verified: the bar disappears when the connection count reaches 0).
- A TCP-only port check and HTTP GETs do not trigger a dialog.
- The bar is a GlobalConfirmInfoBar, shown while 1 or more approved connections exist. It has a "Turn off in settings" button and a Close button.
- Staleness (verified on my own Chrome):
  - DevToolsActivePort stays after kill AND after a graceful Browser.close. The port is then not listening.
  - The source has no delete code.
  - On restart in approval mode, Chrome REUSES the port from the stale file and writes a new GUID (62573 → 62573).
- Port → owner: psutil.net_connections(kind="tcp") mapped the port to the right chrome.exe PID without admin (verified).
- chrome://inspect UI (inspect.html, not localized):
  - checkbox label "Allow remote debugging for this browser instance";
  - after enabling it shows "Server running at: <addr>".
- Command line chrome:// URLs are REJECTED. startup/url_util.cc ValidateLaunchUrlWebUnsafe only allows:
  - web-safe schemes, file: and about:blank;
  - chrome://settings/resetProfileSettings;
  - headless mode with a flag.
  - Experiment: running chrome.exe --user-data-dir=<my profB> "chrome://inspect/#remote-debugging" against my running instance opened a NEW WINDOW with "New Tab" and not the inspect page. Doing this against the user's Chrome would only pop up an empty window. So: copy the URL to the clipboard and tell the user to paste it.
- Edge 154 (own profE): the same pref turns on approval mode (DevToolsActivePort written, /json 404). I did not check whether edge://inspect shows the toggle. Brave is not installed.
- Per-channel dirs: puppeteer resolveDefaultUserDataDir and chrome-devtools-mcp do the same thing: read DevToolsActivePort, then connect to `ws://127.0.0.1:${port}${path}` with no /json probing. Windows paths:
  - Chrome: %LOCALAPPDATA%\Google\Chrome\User Data
  - Chrome Beta: Google\Chrome Beta\User Data
  - Chrome Dev: Google\Chrome Dev\User Data
  - Chrome Canary: Google\Chrome SxS\User Data
  - Edge: Microsoft\Edge\User Data
  - Brave: BraveSoftware\Brave-Browser\User Data
  - agent-browser issue #1210: probing /json first costs an extra prompt. Issue #2041: deleting the port file while a prompt is pending breaks other clients, so never delete it.

=== 2. SeleniumBase cdp_driver cannot be used for attach mode ===
- Browser.start() always GETs http://host:port/json/version (browser.py:674-709). connect_existing only skips the launch (browser.py:615-620).
- Verified: cdp_util.start_async(host, port) against approval-mode Chrome raised "Failed to connect to the browser" after 17.6s.
- Per-tab Connection objects use ws://host:port/devtools/page/<id> (browser.py:246-250, 882-891), which gives 403.
- The Listener ignores sessionId (connection.py:575-665), so flatten sessions are not supported.
- Browser.stop() terminates _process and rmtrees a non-custom user_data_dir (browser.py:925-1072). The atexit deconstruct_browser (browser.py:39-74) also rmtrees.
- Conclusion: use our own small client on `websockets` + mycdp (both already installed). Do not use the SeleniumBase Browser, its Connection, or Browser.stop in attach mode.
- websockets 17.2 defaults to change: open_timeout=10, proxy=True, ping_interval=20, max_size=1MiB. Use open_timeout=60, proxy=None, ping_interval=None, max_size=2**28 (and compression=None). No Origin header is sent by default; Chrome would 403 a foreign Origin (devtools_http_handler.cc:837-856).

=== 3. Working code (verified end to end against my own Chrome over the browser socket only) ===
Scratch prototype: <scratch>\attach\attach_proto.py (find_endpoint, AttachClient, AttachedTab, AttachedBrowser, patch_safe).
exp_d.py drove the REAL pcm Session methods through it, and all of these worked:
- _ensure_prepared (Page.enable, Network.enable+ResponseReceived handler, setBypassCSP, addScriptToEvaluateOnNewDocument(page.js), evaluate page.js);
- _poll_tab (2 jobs, chapter title found);
- _get_bytes (via "cache" = Page.getResourceContent, 184157 bytes) and _screenshot (Page.captureScreenshot clip);
- network handler captured 4 responses;
- _active_tab; opener tracking; stop.
Core:
```python
ws = await websockets.connect(f"ws://127.0.0.1:{port}{path}", open_timeout=60, ping_interval=None, proxy=None, max_size=2**28, compression=None)
# send: req = next(cmd); req["id"] = n; req["sessionId"] = sid (if any); ws.send(json.dumps(req)); on reply cmd.send(msg["result"]) -> StopIteration.value
# recv: msg with "id" -> resolve the future; otherwise ev = mycdp.util.parse_json_event(msg); route by msg.get("sessionId") (None = browser-level)
sid = await send(cdp.target.attach_to_target(target_id, flatten=True))
await send(cdp.runtime.evaluate(expression=..., return_by_value=True, allow_unsafe_eval_blocked_by_csp=True), sid)
await send(cdp.target.set_discover_targets(discover=True))      # Target.targetCreated/infoChanged/destroyed on the browser session
tid = await send(cdp.target.create_target(url))                  # URL mode
await send(cdp.target.detach_from_target(session_id=sid)); await ws.close()   # never Browser.close / Target.closeTarget
```
pcm/cdp.py only needs a branch:
- call(): if the tab is an AttachedTab, use tab.client.send(cmd, tab.session_id, timeout).
- socket_id(): must return a stable per-session value. The current id(tab.websocket) would be id(None), and _ensure_prepared treats that as never prepared (session.py:396).

=== 4. Active tab with 2+ windows (VERIFIED parts) ===
- document.hasFocus() is true only for the active tab of the focused Chrome window. Verified with 2 windows side by side: window 2 → visible + hasFocus True, window 1 → hasFocus False.
- After "Allow", that tab had hasFocus=True (verified with 1 window). The dialog opens on the last active window, Chrome activates that window, and the user's click on Allow leaves it focused. So hasFocus right after the handshake is the best signal for "the tab in front".
- visibilityState is NOT reliable alone. Window 1 reported "hidden" both when covered by my window 2 and when side by side (probably covered by the user's other apps), because native occlusion tracking marks covered windows hidden.
- Target.getTargets order is not activity order (source target_handler.cc:1447-1475 does not sort; the observed order matched neither creation nor activity).
- Target.activateTarget did not raise a background window (Windows blocks focus stealing).
- Browser.getWindowForTarget gives windowId and bounds/state only, not z-order.

=== 5. Opener, navigation, createTarget, CSP, detach (VERIFIED) ===
- Target.targetCreated for tabs opened from the tracked tab carries openerId == tracked targetId at creation, with url "" (filled in later by Target.targetInfoChanged). This holds for an <a target=_blank> click (noopener by default), window.open() and window.open(...,'noopener'); canAccessOpener=false for the noopener ones. A tab created by Target.createTarget (unrelated, like the user opening Gmail) has no openerId and was not tracked.
- Navigations of the tracked tab: Page.frameNavigated (on its session) plus Target.targetInfoChanged (browser level), then domContentEventFired and loadEventFired. A cross-site navigation (127.0.0.1 → localhost) KEEPS the same flatten session, and addScriptToEvaluateOnNewDocument still runs (window.__probe=42 present).
- Target.createTarget(url) opens a NEW FOREGROUND TAB in the last-used profile's last active window; new_window is ignored by Chrome's delegate (chrome_devtools_manager_delegate.cc CreateNewTarget). Observed: it opened in the last active window and was visible.
- Page.setBypassCSP on an already-loaded document does NOT help. On a page with CSP img-src 'self', a blob: image was "blocked" before, still "blocked" after setBypassCSP, and "loaded" after Page.reload. page.js uses blob: URLs (page.js:311), so the existing "press F5" CSP warning (session.py:587-591) is what attach mode needs.
- Detaching and closing the socket left every tab open (4 pages before = 4 after). page.js survives: typeof __gm.toggle == "function", so Alt+T (page.js:364) keeps working after the app leaves.
- DevToolsActivePort + listening port + psutil owner check found my instance correctly.

=== Project pointers ===
- pcm/session.py:
  - :376-390 _launch (SeleniumBase own mode);
  - :365-372 finally → browser.stop() + rmtree _profile_tmp;
  - :234-240 atexit _cleanup_files (tmpdir + _profile_tmp only);
  - :337-348 main_tab + _navigate;
  - :452-456 _browser_alive (uses _process);
  - :458-487 _live_tabs/_prune_tabs/_active_tab (work unchanged when browser.tabs = tracked tabs only).
- pcm/cdp.py:26-57.
- poomcatomanga.py:
  - :751-768 start() requires a URL;
  - :360 keep_browser_profile toggle;
  - :829-836 on_close joins the session for 8s.
- pcm/config.py:93 (last_url), :126 (keep_browser_profile).
- pcm/typeset.py:314-335: the renderer uses its own SeleniumBase headless Chrome, unaffected.
- Scratch experiment scripts: cdpmini.py, exp_a.py, exp_b_start.py, exp_b_ws.py, exp_b_watch.py, exp_c.py, exp_d.py, uia.ps1, uia_watch.ps1, and the fetched sources (v154_*.cc, inspect.html, url_util.cc, BrowserManager.ts, BrowserConnector.ts, chrome.ts) in the same scratch folder.

### Risks

- ACCIDENTAL SIDE EFFECT: running `chrome.exe --version` without --user-data-dir was handed to the user's running Chrome, so an extra empty window or tab may have opened there. Nothing was closed. Tell the user.
- A person at the machine clicked Allow on my test Chrome's permission dialogs. UIA InvokePattern cannot press Chrome's dialog buttons. So the deny path (HTTP 403 'Connection rejected') and 'dialog appears in the last active window' are confirmed from Chrome 154 source, not by experiment.
- A stale dialog stays open after the client gives up (timeout, Stop, or app exit). If the user clicks Allow later, the 'controlled by automated test software' bar stays in every window with no client connected (verified). Fix: tell the user to press Cancel (ยกเลิก) on timeout/Stop. Restarting Chrome or the bar's Close button clears it.
- Every websocket connection prompts again. The app must hold ONE socket for the whole session, never make probe connections, and never auto-reconnect silently. A dropped socket should end the session with a message.
- Windows focus-stealing rules: while PoomCatoManga is in front, Chrome's Activate() for the dialog may only flash in the taskbar. Calling user32.AllowSetForegroundWindow(-1) from the Tk process just before connecting should help, but this is unverified (my test process was never the foreground process).
- Picking the front tab relies on document.hasFocus() right after Allow. If the user switches back to the app quickly, or the dialog shows in another window/profile (last active browser of any profile), it can pick the wrong tab. visibilityState is affected by occlusion, and getTargets order is meaningless. Fallback needed: one visible candidate, otherwise ask the user to click the manga tab and poll hasFocus.
- Evaluating every open tab to find the front one also touches unrelated tabs (Gmail etc.) read-only. Discarded or frozen background tabs may time out; keep timeouts short (2-3 s) and detach right after. How discarded tabs behave was not tested.
- Following navigations: if the user sends the tracked tab to an unrelated site, the page agent keeps scanning and translating there (same as own mode today).
- SeleniumBase must not be used for the user's Chrome. Its Browser.stop() terminates processes and rmtrees user_data_dir, and its atexit deconstruct_browser runs for every registered instance. Attach code must never call browser.stop() from the own-mode path or touch _profile_tmp.
- websockets.connect defaults (open_timeout=10, proxy=True, max_size=1MiB) break attach mode. Pass open_timeout≈60, proxy=None, max_size=2**28, ping_interval=None explicitly.
- A stale DevToolsActivePort can point to a port another program now uses. Check the listening PID's exe name with psutil (chrome.exe/msedge.exe/brave.exe) before connecting. Never delete the file.
- Enterprise policy RemoteDebuggingAllowed=false stops the server from starting even with the box ticked. The guide should mention it when the file never appears.
- Edge: the approval-mode pref works on 154, but I did not check whether edge://inspect shows the toggle, or Edge's dialog wording. Brave is untested.
- Network.enable with 200MB buffers and page.js injection now run inside the user's everyday browser for every tracked tab. Memory use goes up while attached.
- Chrome may change this feature. A 'remember approval' request (chrome-devtools-mcp #825) was closed as not planned. Behaviour was checked on 154.0.8037.98 only.

### Recommended design

Implement attach mode as a separate path. Own mode stays byte-for-byte as today.

1. New module pcm/attach.py (port of the verified scratch attach_proto.py):
- find_endpoint(): go through USER_DATA_DIRS in order:
  - Chrome stable, Beta, Dev, Canary (SxS), Edge, Brave.
  - Parse DevToolsActivePort (line 1 = port, line 2 must start with /devtools/browser).
  - Accept a browser only if a TCP connect to 127.0.0.1:port succeeds AND psutil shows the listener's exe is chrome.exe/msedge.exe/brave.exe.
  - Return (label, port, path). Never delete or write the file.
- AttachClient: one `websockets.connect(f"ws://127.0.0.1:{port}{path}", open_timeout=60, ping_interval=None, proxy=None, max_size=2**28, compression=None)`.
  - Requests are mycdp generators with sessionId added.
  - The receive loop resolves replies by id. It parses events with mycdp.util.parse_json_event and routes them by sessionId to that AttachedTab's handlers, or to browser-level handlers.
  - On close, it fails pending futures and sets `closed`.
- AttachedTab: target_id, session_id, add_handler(type, fn), send(cmd, timeout). It is duck-typed like a SeleniumBase Tab.
- AttachedBrowser:
  - Keeps `tracked` {tid: AttachedTab}; `tabs` returns tracked tabs only; main_tab; `_process = None`; alive() = not client.closed.
  - start(url): Target.setDiscoverTargets(true). With a URL: Target.createTarget(url), which opens a foreground tab in the last active window. Without a URL: front_tab().
  - front_tab(): for each http/https/file page, briefly attach, evaluate [visibilityState, hasFocus()], detach. Order: first hasFocus; else the only visible tab; else None, and the UI says "คลิกที่แท็บมังงะใน Chrome" while polling hasFocus for about 15 s.
  - TargetCreated: if opener_id is in tracked, attach and track it. TargetDestroyed: drop it.
  - update_targets() is a no-op.
  - stop(): Target.detachFromTarget for each session, then ws.close(). Never Browser.close, Target.closeTarget or any process kill.

2. pcm/cdp.py:
- call(): if the tab has a session_id, use `return await tab.send(cmd, timeout)` (wrap errors as CDPError); otherwise the current code.
- socket_id(): return hash((id(tab.client), tab.session_id)) for an AttachedTab.

3. pcm/session.py (only branch on self.s.browser_mode == "attach"):
- _main: log DETAIL.info("โหมดเบราว์เซอร์: attach %s port=%d", label, port) and never log the ws path or GUID. Call `await self._attach()` instead of `_launch()`. Skip `_navigate`, because createTarget already loads the URL and an empty URL means "use the current page". After start, log the attached tab URL.
- _attach(): find_endpoint(). If none: emit ("attach_help",) and raise a friendly error. Otherwise:
  - emit ("status", "กด Allow (อนุญาต) ในหน้าต่าง Chrome เพื่ออนุญาต");
  - create the connect task;
  - wait for whichever comes first: the connect task, or stop_event.
  - Map the outcomes:
    - InvalidStatus 403 → "Chrome ไม่อนุญาต (กด Cancel/ปิดหน้าต่าง หรือไม่มีหน้าต่าง Chrome เปิดอยู่)";
    - TimeoutError → "หมดเวลา 60 วิ — ถ้ายังเห็นหน้าต่างขออนุญาตใน Chrome ให้กด Cancel";
    - Stop pressed → cancel the task and show the same Cancel hint;
    - ConnectionRefused → stale → the help message.
  - Every failure path closes everything, so nothing is left half-started.
- _browser_alive(): in attach mode use browser.alive(). If the socket drops: "การเชื่อมต่อกับ Chrome หลุด — หยุดการแปล". No silent reconnect (it would prompt again).
- finally: in attach mode `await asyncio.wait_for(self.browser.stop(), 5)`, never SeleniumBase stop. `_profile_tmp` stays None, so the atexit _cleanup_files only removes the session tmpdir.
- The renderer and everything after _get_bytes stay unchanged. The CSP F5 warning already covers already-loaded pages (setBypassCSP does not affect the current document; verified).

4. Settings (pcm/config.py): add `browser_mode: str = "own"`, valid values "own" | "attach". Validate on load, and fall back to "own".

5. UI (poomcatomanga.py):
- Card 1: a Checkbutton "ใช้ Chrome ที่เปิดอยู่ (ไม่ต้องวางลิงก์)" bound to browser_mode.
- In attach mode:
  - start() no longer requires a URL (poomcatomanga.py:756). Placeholder/hint: "เว้นว่าง = แปลแท็บที่เปิดอยู่ด้านหน้า / ใส่ลิงก์ = เปิดแท็บใหม่".
  - Disable keep_browser_profile and the cookie/profile options, with a tooltip saying they don't apply.
- On ("attach_help",), show a Thai dialog:
  - "เปิด Chrome แล้ววางลิงก์ chrome://inspect/#remote-debugging ในช่องที่อยู่ → ติ๊ก 'Allow remote debugging for this browser instance' (ทำครั้งเดียว ใช้ได้ถึงตอนปิดติ๊ก) แล้วกดเริ่มอีกครั้ง".
  - Add a button "คัดลอกลิงก์" that does root.clipboard_clear() and clipboard_append(url), then says "คัดลอกแล้ว — วางในแถบที่อยู่ของ Chrome".
  - Do NOT launch chrome.exe with that URL. Chrome rejects chrome:// on the command line and would only open an empty New Tab window (verified).
- Optionally call ctypes.windll.user32.AllowSetForegroundWindow(-1) right before Start in attach mode so Chrome's dialog can come to the front (unverified best effort).

6. Logging: DETAIL records the mode, browser label and port, the chosen tab URL, and each tracked opener tab URL. It never records the ws GUID.

7. Tests:
- A unit test for find_endpoint using temp dirs: stale file, bad format, live port.
- A tests/test_attach.py that launches its own Chrome with --remote-debugging-port=0 and a fresh --user-data-dir. It drives AttachClient over the browser socket only, exactly like exp_d.py, because classic mode accepts the same browser-socket and flatten-session protocol.
- The approval dialog itself needs a manual check.
