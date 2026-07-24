/** IM bridge ("Connect Phone") modal — manage Feishu / Telegram channels.
 *
 * Redesigned UI: modern card layout, platform brand colors, status pills,
 * empty state illustration, and a two-step "add robot" flow. All server
 * values are rendered via textContent; trusted SVG icons are parsed through
 * DOMParser.
 */

import * as api from "../api";
import { channelIconHtml } from "../icons";
import * as i18n from "../i18n";
import { closeAllFullpageOverlays } from "../modals";

function channelLabel(name: string): string {
  return name === "feishu" ? i18n.t("im.feishu")
    : name === "telegram" ? "Telegram"
    : name;
}

function channelHint(name: string): string {
  return name === "feishu" ? i18n.t("im.hint.feishu")
    : name === "telegram" ? i18n.t("im.hint.telegram")
    : "";
}

function stateLabel(state: string): string {
  switch (state) {
    case "connected": return i18n.t("im.state.connected");
    case "connecting": return i18n.t("im.state.connecting");
    case "waiting_scan": return i18n.t("im.state.waitingScan");
    case "error": return i18n.t("im.state.error");
    case "disconnected": return i18n.t("im.state.disconnected");
    default: return state;
  }
}

function svgNode(svg: string): SVGSVGElement | null {
  if (!svg) return null;
  try {
    const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
    const node = doc.documentElement;
    return node instanceof SVGSVGElement ? node : null;
  } catch {
    return null;
  }
}

function setIconSize(node: SVGSVGElement | null, size: number): SVGSVGElement | null {
  if (!node) return null;
  node.setAttribute("width", String(size));
  node.setAttribute("height", String(size));
  return node;
}

export async function openIMBridgeModal(): Promise<void> {
  closeAllFullpageOverlays();
  const backdrop = document.createElement("div");
  backdrop.className = "fullpage-overlay";
  backdrop.id = "imBridgeModalBackdrop";
  backdrop.innerHTML = `
    <div class="fullpage-shell">
      <div class="fullpage-topbar">
        <div class="fullpage-title">${i18n.t("im.title")}</div>
        <div class="fullpage-topbar-spacer"></div>
      </div>
      <div class="fullpage-body" id="imBridgeModalBody">
        <div class="skills-modal-loading">${i18n.t("im.loading")}</div>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  const closeBtn = document.createElement("button");
  closeBtn.className = "im-close-btn";
  closeBtn.textContent = "×";
  closeBtn.title = i18n.t("common.close");
  closeBtn.onclick = () => closeIMBridgeModal();
  backdrop.querySelector(".fullpage-topbar")?.appendChild(closeBtn);
  await loadIMBridgeIntoModal();
}

export function closeIMBridgeModal(): void {
  document.getElementById("imBridgeModalBackdrop")?.remove();
}

async function loadIMBridgeIntoModal(): Promise<void> {
  const body = document.getElementById("imBridgeModalBody");
  if (!body) return;
  body.textContent = "";
  const loading = document.createElement("div");
  loading.className = "skills-modal-loading";
  loading.textContent = i18n.t("im.loading");
  body.appendChild(loading);
  try {
    const [channels, config, pending] = await Promise.all([
      api.listIMChannels(),
      api.getIMConfig(),
      api.listPendingSenders().catch(() => ({ senders: [] })),
    ]);
    renderMain(body, channels, config, pending.senders);
  } catch (e: any) {
    body.textContent = "";
    const err = document.createElement("div");
    err.className = "im-empty-state";
    err.textContent = i18n.t("im.loadFailed", { err: e?.message || e });
    body.appendChild(err);
  }
}

function renderMain(
  body: HTMLElement,
  channels: api.IMChannelStatus[],
  config: api.IMConfigPublic,
  pendingSenders: api.IMPendingSender[]
): void {
  body.textContent = "";
  const container = document.createElement("div");
  container.className = "im-container";

  const intro = document.createElement("div");
  intro.className = "im-intro";
  intro.textContent = i18n.t("im.intro");
  container.appendChild(intro);

  const connected = channels.filter((c) => c.state === "connected" || c.state === "connecting" || c.state === "waiting_scan");
  const configured = channels.filter((c) => c.configured && !connected.includes(c));
  const rest = channels.filter((c) => !connected.includes(c) && !configured.includes(c));
  const hasAny = connected.length > 0 || configured.length > 0;

  if (!hasAny) {
    container.appendChild(renderEmptyState());
  } else {
    const sectionHeader = document.createElement("div");
    sectionHeader.className = "im-section-header";
    const title = document.createElement("div");
    title.className = "im-section-title";
    title.textContent = connected.length > 0 ? i18n.t("im.connectedBots", { n: connected.length }) : i18n.t("im.bots");
    const addBtn = document.createElement("button");
    addBtn.className = "im-add-btn";
    addBtn.innerHTML = `<span>+</span><span>${i18n.t("im.addBot")}</span>`;
    addBtn.onclick = () => openAddRobotOverlay();
    sectionHeader.append(title, addBtn);
    container.appendChild(sectionHeader);

    for (const ch of connected) container.appendChild(renderChannelCard(ch));
    for (const ch of configured) container.appendChild(renderChannelCard(ch));
    for (const ch of rest) container.appendChild(renderChannelCard(ch));
  }

  container.appendChild(renderConfigSection(config, pendingSenders));
  body.appendChild(container);
}

function renderEmptyState(): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "im-empty-state";
  const icon = svgNode(`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="2" width="10" height="16" rx="2"/><path d="M11 18v3"/><path d="M9 21h6"/><path d="M12 5v.01"/><path d="M8 14h8"/></svg>`);
  const iconWrap = document.createElement("div");
  iconWrap.className = "im-empty-icon";
  if (icon) iconWrap.appendChild(icon);
  const title = document.createElement("div");
  title.className = "im-empty-title";
  title.textContent = i18n.t("im.emptyTitle");
  const desc = document.createElement("div");
  desc.className = "im-empty-desc";
  desc.textContent = i18n.t("im.emptyDesc");
  const btn = document.createElement("button");
  btn.className = "im-add-btn";
  btn.innerHTML = `<span>+</span><span>${i18n.t("im.addBot")}</span>`;
  btn.onclick = () => openAddRobotOverlay();
  wrap.append(iconWrap, title, desc, btn);
  return wrap;
}

function renderChannelCard(ch: api.IMChannelStatus): HTMLElement {
  const card = document.createElement("div");
  card.className = "im-card";

  const iconWrap = document.createElement("div");
  iconWrap.className = "im-card-icon";
  const node = setIconSize(svgNode(channelIconHtml(ch.name)), 22);
  if (node) iconWrap.appendChild(node);
  card.appendChild(iconWrap);

  const main = document.createElement("div");
  main.className = "im-card-main";

  const titleRow = document.createElement("div");
  titleRow.className = "im-card-title-row";
  const title = document.createElement("div");
  title.className = "im-card-title";
  title.textContent = channelLabel(ch.name);
  titleRow.appendChild(title);
  titleRow.appendChild(renderStatusPill(ch.state, ch.error));

  const sub = document.createElement("div");
  sub.className = "im-card-sub";
  if (ch.state === "error" && ch.error) {
    sub.classList.add("error");
    sub.textContent = ch.error;
  } else if (ch.state === "connected" && ch.account_id) {
    sub.textContent = ch.account_id;
  } else if (!ch.configured) {
    sub.textContent = channelHint(ch.name) || i18n.t("im.notConfigured");
  } else {
    sub.textContent = channelHint(ch.name);
  }
  main.append(titleRow, sub);
  card.appendChild(main);

  const actions = document.createElement("div");
  actions.className = "im-card-actions";
  const active = ch.state === "connected" || ch.state === "connecting" || ch.state === "waiting_scan";
  if (active) {
    const btn = document.createElement("button");
    btn.className = "im-btn danger";
    btn.textContent = i18n.t("im.disconnect");
    btn.onclick = () => void disconnect(ch.name);
    actions.appendChild(btn);
  } else {
    const btn = document.createElement("button");
    btn.className = "im-btn primary";
    btn.textContent = i18n.t("im.connect");
    btn.onclick = () => openAddRobotOverlay(ch.name);
    actions.appendChild(btn);
  }
  card.appendChild(actions);
  return card;
}

function renderStatusPill(state: string, error: string | null): HTMLElement {
  const pill = document.createElement("span");
  pill.className = `im-status-pill ${state}`;
  pill.textContent = stateLabel(state);
  return pill;
}

function openAddRobotOverlay(preselect?: string): void {
  const backdrop = document.getElementById("imBridgeModalBackdrop");
  if (!backdrop) return;
  const existing = document.getElementById("imBridgeAddOverlay");
  if (existing) existing.remove();

  const overlay = document.createElement("div");
  overlay.className = "im-add-overlay";
  overlay.id = "imBridgeAddOverlay";
  overlay.innerHTML = `
    <div class="fullpage-topbar">
      <button class="fullpage-back" id="imBridgeAddBack">${i18n.t("im.back")}</button>
      <div class="fullpage-title">${i18n.t("im.addBot")}</div>
      <div class="fullpage-topbar-spacer"></div>
    </div>
    <div class="im-add-body" id="imBridgeAddBody"></div>`;
  backdrop.appendChild(overlay);

  const backBtn = overlay.querySelector("#imBridgeAddBack") as HTMLButtonElement;
  backBtn.onclick = () => overlay.remove();

  const body = document.getElementById("imBridgeAddBody");
  if (!body) return;
  if (preselect) {
    renderPlatformForm(body, preselect);
  } else {
    renderPlatformGrid(body);
  }
}

function renderPlatformGrid(body: HTMLElement): void {
  body.textContent = "";
  const inner = document.createElement("div");
  inner.className = "im-add-inner";
  const hint = document.createElement("div");
  hint.className = "im-intro";
  hint.textContent = i18n.t("im.platformHint");
  inner.appendChild(hint);

  const grid = document.createElement("div");
  grid.className = "im-platform-grid";
  for (const name of ["feishu", "telegram"] as const) {
    const card = document.createElement("div");
    card.className = "im-platform-card";
    const icon = setIconSize(svgNode(channelIconHtml(name)), 48);
    const iconWrap = document.createElement("div");
    iconWrap.className = "platform-icon";
    if (icon) iconWrap.appendChild(icon);
    const title = document.createElement("div");
    title.className = "platform-name";
    title.textContent = channelLabel(name);
    const sub = document.createElement("div");
    sub.className = "platform-hint";
    sub.textContent = channelHint(name);
    card.append(iconWrap, title, sub);
    card.onclick = () => renderPlatformForm(body, name);
    grid.appendChild(card);
  }
  inner.appendChild(grid);
  body.appendChild(inner);
}

function renderPlatformForm(body: HTMLElement, name: string): void {
  body.textContent = "";
  const inner = document.createElement("div");
  inner.className = "im-add-inner";

  const header = document.createElement("div");
  header.className = "im-platform-form-header";
  const icon = setIconSize(svgNode(channelIconHtml(name)), 32);
  const iconWrap = document.createElement("div");
  iconWrap.className = "platform-icon";
  if (icon) iconWrap.appendChild(icon);
  const title = document.createElement("div");
  title.className = "im-section-title";
  title.textContent = name === "telegram" ? i18n.t("im.connectTelegram") : i18n.t("im.connectFeishu");
  header.append(iconWrap, title);
  inner.appendChild(header);

  const form = document.createElement("div");
  form.className = "im-form";
  const fields: { key: string; label: string; type?: string }[] =
    name === "feishu"
      ? [
          { key: "app_id", label: i18n.t("im.field.appId") },
          { key: "app_secret", label: i18n.t("im.field.appSecret"), type: "password" },
        ]
      : name === "telegram"
      ? [
          { key: "bot_token", label: i18n.t("im.field.botToken"), type: "password" },
          { key: "proxy_url", label: i18n.t("im.field.proxyUrl") },
        ]
      : [];

  const inputs: Record<string, HTMLInputElement> = {};
  for (const f of fields) {
    const label = document.createElement("div");
    label.className = "im-form-label";
    label.textContent = f.label;
    const inp = document.createElement("input");
    inp.type = f.type || "text";
    inp.placeholder = f.label;
    inputs[f.key] = inp;
    form.append(label, inp);
  }

  const errorEl = document.createElement("div");
  errorEl.className = "im-form-error";
  form.appendChild(errorEl);

  const actions = document.createElement("div");
  actions.className = "im-form-actions";
  const submit = document.createElement("button");
  submit.className = "im-btn primary";
  submit.textContent = i18n.t("im.verifyAndConnect");
  const cancel = document.createElement("button");
  cancel.className = "im-btn ghost";
  cancel.textContent = i18n.t("common.cancel");
  cancel.onclick = () => document.getElementById("imBridgeAddOverlay")?.remove();
  submit.onclick = async () => {
    errorEl.classList.remove("visible");
    errorEl.textContent = "";
    const payload: Record<string, string> = {};
    for (const f of fields) {
      const v = inputs[f.key].value.trim();
      if (v) payload[f.key] = v;
      else {
        errorEl.textContent = i18n.t("im.fillField", { field: f.label });
        errorEl.classList.add("visible");
        return;
      }
    }
    submit.disabled = true;
    const originalText = submit.textContent;
    submit.innerHTML = `<span class="im-spinner"></span>${i18n.t("im.connecting")}`;
    try {
      const res = await api.startIMChannel(name, payload);
      if (res && res.error) {
        errorEl.textContent = res.message || res.error;
        errorEl.classList.add("visible");
        submit.disabled = false;
        submit.textContent = originalText;
        return;
      }
      document.getElementById("imBridgeAddOverlay")?.remove();
      await loadIMBridgeIntoModal();
    } catch (e: any) {
      errorEl.textContent = e?.message || String(e);
      errorEl.classList.add("visible");
      submit.disabled = false;
      submit.textContent = originalText;
    }
  };
  actions.append(cancel, submit);
  form.appendChild(actions);
  inner.appendChild(form);
  body.appendChild(inner);
}

async function disconnect(name: string): Promise<void> {
  try {
    await api.stopIMChannel(name);
    await loadIMBridgeIntoModal();
  } catch (e: any) {
    alert(i18n.t("im.disconnectFailed", { err: e?.message || e }));
  }
}

function renderConfigSection(config: api.IMConfigPublic, pendingSenders: api.IMPendingSender[]): HTMLElement {
  const sec = document.createElement("div");
  sec.className = "im-config-section";

  const h = document.createElement("div");
  h.className = "im-section-title";
  h.textContent = i18n.t("im.allowlistTitle");
  sec.appendChild(h);

  const hint = document.createElement("div");
  hint.className = "im-config-hint";
  hint.innerHTML = i18n.t("im.allowlistHint");
  sec.appendChild(hint);

  if (pendingSenders.length > 0) {
    const pendingH = document.createElement("div");
    pendingH.className = "im-config-subtitle";
    pendingH.textContent = i18n.t("im.pendingTitle");
    sec.appendChild(pendingH);

    const pendingList = document.createElement("div");
    pendingList.className = "im-pending-list";
    for (const s of pendingSenders) {
      const row = document.createElement("div");
      row.className = "im-pending-row";
      const info = document.createElement("div");
      info.className = "im-pending-info";
      info.textContent = i18n.t("im.pendingSender", { name: s.sender_name || s.sender_id, channel: channelLabel(s.channel) });
      const idEl = document.createElement("code");
      idEl.textContent = s.sender_id;
      const approve = document.createElement("button");
      approve.className = "im-btn primary";
      approve.textContent = i18n.t("im.allow");
      approve.onclick = () => void approveAndReload(s.sender_id);
      row.append(info, idEl, approve);
      pendingList.appendChild(row);
    }
    sec.appendChild(pendingList);
  }

  const listH = document.createElement("div");
  listH.className = "im-config-subtitle";
  listH.textContent = i18n.t("im.allowed");
  sec.appendChild(listH);

  const chipList = document.createElement("div");
  chipList.className = "im-chip-list";
  for (const sender of config.allowed_senders) {
    const chip = document.createElement("div");
    chip.className = "im-chip";
    const txt = document.createElement("span");
    txt.textContent = sender;
    const rm = document.createElement("button");
    rm.className = "im-chip-remove";
    rm.textContent = "×";
    rm.title = i18n.t("im.removeSender");
    rm.onclick = () => void updateSenders(config.allowed_senders.filter((s) => s !== sender));
    chip.append(txt, rm);
    chipList.appendChild(chip);
  }
  if (config.allowed_senders.length === 0) {
    const empty = document.createElement("div");
    empty.className = "im-config-hint";
    empty.textContent = i18n.t("im.noAllowlist");
    chipList.appendChild(empty);
  }
  sec.appendChild(chipList);

  const addRow = document.createElement("div");
  addRow.className = "im-whitelist-add";
  const inp = document.createElement("input");
  inp.placeholder = i18n.t("im.senderIdPlaceholder");
  const addBtn = document.createElement("button");
  addBtn.className = "im-btn primary";
  addBtn.textContent = i18n.t("common.add");
  addBtn.onclick = async () => {
    const v = inp.value.trim();
    if (!v) return;
    if (config.allowed_senders.includes(v)) return;
    await updateSenders([...config.allowed_senders, v]);
  };
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") (addBtn.onclick as () => void)();
  });
  addRow.append(inp, addBtn);
  sec.appendChild(addRow);

  const wsH = document.createElement("div");
  wsH.className = "im-section-title";
  wsH.style.marginTop = "24px";
  wsH.textContent = i18n.t("im.defaultWorkspace");
  sec.appendChild(wsH);
  const wsHint = document.createElement("div");
  wsHint.className = "im-config-hint";
  wsHint.textContent = i18n.t("im.workspaceHint");
  sec.appendChild(wsHint);

  const wsRow = document.createElement("div");
  wsRow.className = "im-workspace-row";
  const wsInp = document.createElement("input");
  wsInp.placeholder = i18n.t("im.workspacePlaceholder");
  wsInp.value = config.default_workspace || "";
  const chooseBtn = document.createElement("button");
  chooseBtn.className = "im-btn";
  chooseBtn.textContent = i18n.t("im.choose");
  chooseBtn.onclick = async () => {
    try {
      const res = await api.chooseSystemFolder();
      if (res.path) wsInp.value = res.path;
    } catch (e: any) {
      alert(i18n.t("im.chooseFailed", { err: e?.message || e }));
    }
  };
  const saveBtn = document.createElement("button");
  saveBtn.className = "im-btn primary";
  saveBtn.textContent = i18n.t("common.save");
  saveBtn.onclick = async () => {
    try {
      await api.updateIMConfig({ default_workspace: wsInp.value.trim() || null });
      saveBtn.textContent = i18n.t("common.saved");
      setTimeout(() => (saveBtn.textContent = i18n.t("common.save")), 1500);
    } catch (e: any) {
      alert(i18n.t("im.saveFailed", { err: e?.message || e }));
    }
  };
  wsRow.append(wsInp, chooseBtn, saveBtn);
  sec.appendChild(wsRow);

  return sec;
}

async function approveAndReload(senderId: string): Promise<void> {
  try {
    await api.approvePendingSender(senderId);
    await loadIMBridgeIntoModal();
  } catch (e: any) {
    alert(i18n.t("im.allowApproveFailed", { err: e?.message || e }));
  }
}

async function updateSenders(allowed_senders: string[]): Promise<void> {
  try {
    await api.updateIMConfig({ allowed_senders });
    await loadIMBridgeIntoModal();
  } catch (e: any) {
    alert(i18n.t("im.saveFailed", { err: e?.message || e }));
  }
}
