"use strict";
const tg = window.Telegram?.WebApp;
const $ = (id) => document.getElementById(id);
const fmt = (value) => new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 1}).format(value);
const whole = (value) => new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 0}).format(value);
let state, selectedFavorite, editingEntry, favoriteOffset = 0;
let diaryGeneration = 0, desiredDay = "", receivedAt = 0, storageUser = null;
let drafts = {}, baselines = new WeakMap(), toastTimer;
let foodTemplate = null, favoriteGeneration = 0, searchTimer;
let statsGeneration = 0, weightOffset = 0, editingWeight = null;
let mealHead = null, weekGeneration = 0, confirmingClose = false, draftTimer;
let pending = {};
function readStorage(key) { try { const value = JSON.parse(localStorage.getItem(key) || "{}"); return value && typeof value === "object" && !Array.isArray(value) ? value : {}; } catch { return {}; } }
function saveStorage(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } }
function setStorageUser(id) {
  if (storageUser === id) return;
  storageUser = id;
  pending = readStorage(`kcalorie-pending-${id}`);
  drafts = readStorage(`kcalorie-drafts-${id}`);
  const stale = Object.keys(drafts).filter((form) => !draftNames[form]);
  if (stale.length) { stale.forEach((form) => delete drafts[form]); saveStorage(`kcalorie-drafts-${id}`, drafts); }
  renderDrafts();
}
function persistPending() {
  if (storageUser) saveStorage(`kcalorie-pending-${storageUser}`, pending);
}

async function api(path, method = "GET", data) {
  const body = data === undefined ? undefined : JSON.stringify(data);
  const signature = JSON.stringify([path, method, body]);
  const key = method === "GET" ? null : (pending[signature] ||= crypto.randomUUID());
  if (key) persistPending();
  for (let attempt = 0; attempt < 3; attempt++) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/${path}`, {
        method, signal: controller.signal,
        headers: {Authorization: `tma ${tg?.initData || ""}`, "Content-Type": "application/json", ...(key ? {"Idempotency-Key": key} : {})},
        body,
      });
      if (response.status >= 500) throw new Error("Сервер временно недоступен.");
      const result = await response.json();
      if (key && response.status !== 401 && response.status !== 429) { delete pending[signature]; persistPending(); }
      if (!response.ok) {
        const error = new Error(result.error || "Не удалось выполнить запрос.");
        error.status = response.status;
        throw error;
      }
      return result;
    } catch (error) {
      if (error.status || attempt === 2) throw error.status ? error : new Error("Связь прервалась. Повторите отправку: повтор того же запроса не создаст дубль.");
    } finally { clearTimeout(timeout); }
    await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1)));
  }
}
function notice(text = "") {
  clearTimeout(toastTimer);
  if (!state || state.needs_timezone) { $("notice").textContent = text; $("notice").hidden = !text; return; }
  $("notice").hidden = true;
  $("toasts").querySelector(".message-toast")?.remove();
  if (text) {
    const toast = node("div", text, "toast message-toast");
    toast.append(action("Закрыть", () => toast.remove()));
    $("toasts").append(toast);
    toastTimer = setTimeout(() => toast.remove(), 12000);
  }
}
function node(tag, text, className) {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}
function action(text, handler, className = "text-button") {
  const button = node("button", text, className);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await handler(); } catch (error) { notice(error.message); }
    finally { button.disabled = false; }
  });
  return button;
}
const MEALS = [
  {id: "breakfast", name: "Завтрак", from: 5, to: 11},
  {id: "lunch", name: "Обед", from: 11, to: 16},
  {id: "afternoon", name: "Полдник", from: 16, to: 18},
  {id: "dinner", name: "Ужин", from: 18, to: 23},
  {id: "night", name: "Ночной перекус", from: 23, to: 5},
];
function mealOf(epoch) {
  const hour = Number(new Intl.DateTimeFormat("ru-RU", {hour: "2-digit", hourCycle: "h23", timeZone: state.timezone}).format(new Date(epoch * 1000)));
  return MEALS.find(meal => meal.from < meal.to ? hour >= meal.from && hour < meal.to : hour >= meal.from || hour < meal.to);
}
function appendEntry(entry) {
  const meal = mealOf(entry.eaten_at_utc);
  if (mealHead?.dataset.meal !== meal.id) {
    mealHead = node("div", "", "meal-head");
    mealHead.dataset.meal = meal.id;
    mealHead.dataset.total = "0";
    mealHead.append(node("span", meal.name), node("span", "", "meal-total"));
    $("entries").append(mealHead);
  }
  mealHead.dataset.total = Number(mealHead.dataset.total) + entry.nutrition.calories;
  mealHead.lastElementChild.textContent = `${fmt(Number(mealHead.dataset.total))} ккал`;
  $("entries").append(renderEntry(entry));
}
function renderEntry(entry) {
  const card = node("article", "", "entry");
  const name = entry.name || "Без названия";
  const time = new Intl.DateTimeFormat("ru-RU", {hour: "2-digit", minute: "2-digit", timeZone: state.timezone}).format(new Date(entry.eaten_at_utc * 1000));
  const amount = entry.nutrition.servings == null ? `${fmt(entry.nutrition.grams)} г` : `${fmt(entry.nutrition.servings)} порц.`;
  const main = action("", async () => openFood(await api(`entries/${entry.entry_id}`)), "entry-main");
  main.setAttribute("aria-label", `Редактировать: ${name}`);
  main.append(node("span", name, "entry-name"), node("span", `${fmt(entry.nutrition.calories)} ккал`, "entry-kcal"));
  const foot = node("div", "", "entry-foot");
  foot.append(node("span", `${time} · ${amount}`, "entry-meta"));
  card.append(main, foot);
  const controls = node("div", "", "entry-actions");
  if (entry.name) controls.append(action("♡ В избранное", async () => { await api(`entries/${entry.entry_id}/favorite`, "POST", {}); notice("Сохранено в избранное."); }));
  controls.append(action("Удалить", async () => {
    await api(`entries/${entry.entry_id}`, "DELETE", {version: entry.version});
    const toast = node("div", `Удалено: ${entry.name || "Без названия"}`, "toast");
    toast.append(action("Отменить", async () => {
      await api(`entries/${entry.entry_id}/restore`, "POST", {});
      toast.remove();
      await loadDiary(state.day);
    }), action("Закрыть", () => toast.remove()));
    $("toasts").append(toast);
    setTimeout(() => toast.remove(), 900000);
    await loadDiary(state.day);
  }));
  foot.append(controls);
  return card;
}
function capitalize(text) { return text.charAt(0).toUpperCase() + text.slice(1); }
function renderDayLabel() {
  const date = new Date(`${state.day}T12:00:00Z`);
  const previous = new Date(`${state.today}T12:00:00Z`);
  previous.setUTCDate(previous.getUTCDate() - 1);
  const relative = state.day === state.today ? "Сегодня"
    : state.day === previous.toISOString().slice(0, 10) ? "Вчера"
    : new Intl.DateTimeFormat("ru-RU", {weekday: "long", timeZone: "UTC"}).format(date);
  const full = new Intl.DateTimeFormat("ru-RU", {day: "numeric", month: "long", timeZone: "UTC"}).format(date);
  $("day-label").textContent = `${capitalize(relative)}, ${full}`;
}
/* Meal segments need every entry of the day, so they are drawn on a single complete page only. */
function mealTotals() {
  if (state.entries.has_next || state.entries.offset) return null;
  const sums = MEALS.map(() => 0);
  for (const entry of state.entries.items) sums[MEALS.indexOf(mealOf(entry.eaten_at_utc))] += entry.nutrition.calories;
  return sums.map((value, index) => [index, value]).filter(([, value]) => value > 0);
}
function renderTrack(consumed, over) {
  const scale = state.goal ? Math.max(state.goal, consumed) : consumed;
  $("track").classList.toggle("no-goal", !state.goal);
  $("track").setAttribute("aria-valuenow", String(state.goal ? Math.round(Math.min(100, consumed / state.goal * 100)) : 0));
  $("fill").style.width = scale > 0 ? `${Math.min(100, consumed / scale * 100)}%` : "0";
  $("over-mark").hidden = !over;
  if (over) $("over-mark").style.left = `${state.goal / scale * 100}%`;
  const parts = consumed > 0 ? mealTotals() : [];
  $("fill").replaceChildren(...(parts || [[0, consumed]]).map(([index, value]) => {
    const segment = node("i", "", index ? `s${index}` : "");
    segment.style.width = `${parts ? value / consumed * 100 : 100}%`;
    return segment;
  }));
}
/* Shares of the energy the macros account for; hidden unless all three are known. */
function renderMacroSplit(partial) {
  const energy = {protein: 4, fat: 9, carbs: 4};
  const values = Object.entries(energy).map(([key, factor]) => state.stats[key] * factor);
  const total = values.reduce((sum, value) => sum + value, 0);
  $("macro-split").hidden = partial || values.some(value => !Number.isFinite(value)) || !(total > 0);
  if ($("macro-split").hidden) return;
  [...$("macro-split").children].forEach((bar, index) => { bar.style.width = `${values[index] / total * 100}%`; });
}
/* Drawn synchronously so the card never grows under the user once totals arrive. */
function renderWeek() {
  const days = [];
  for (let offset = 6; offset >= 0; offset--) {
    const date = new Date(`${state.day}T12:00:00Z`);
    date.setUTCDate(date.getUTCDate() - offset);
    days.push(date.toISOString().slice(0, 10));
  }
  $("week").replaceChildren(...days.map((day) => {
    const date = new Date(`${day}T12:00:00Z`);
    const button = action("", () => loadDiary(day), "week-day");
    button.dataset.day = day;
    button.disabled = day < state.earliest_day || day > state.today;
    button.setAttribute("aria-label", prettyDay(day));
    if (day === state.day) button.setAttribute("aria-current", "date");
    const bar = node("span", "", "week-bar");
    bar.append(node("i", ""));
    button.append(node("span", new Intl.DateTimeFormat("ru-RU", {weekday: "short", timeZone: "UTC"}).format(date)), bar, node("span", String(date.getUTCDate())));
    return button;
  }));
  $("week").hidden = false;
}
async function loadWeek() {
  const generation = ++weekGeneration;
  try {
    const result = await api(`statistics?${new URLSearchParams({day: state.day, period: "week"})}`);
    if (generation !== weekGeneration) return;
    const base = state.goal || Math.max(1, ...result.days.map(item => item.calories || 0));
    for (const item of result.days) {
      const button = $("week").querySelector(`[data-day="${item.day}"]`);
      if (!button) continue;
      button.setAttribute("aria-label", `${prettyDay(item.day)}: ${item.calories == null ? "нет записей" : `${fmt(item.calories)} ккал`}`);
      const value = button.querySelector("i");
      value.style.height = item.calories ? `${Math.max(8, Math.min(100, item.calories / base * 100))}%` : "0";
      value.classList.toggle("over", Boolean(state.goal && item.calories > state.goal));
    }
  } catch { /* the strip is an aid, not a requirement */ }
}
async function loadDiary(day = "", append = false) {
  const generation = ++diaryGeneration;
  desiredDay = day;
  $("entries").setAttribute("aria-busy", "true");
  $("more").disabled = true;
  try {
    const offset = append ? state.entries.offset + state.entries.items.length : 0;
    const query = new URLSearchParams({offset: String(offset)});
    if (day) query.set("day", day);
    const result = await api(`diary?${query}`);
    if (generation !== diaryGeneration) return;
    state = result;
    receivedAt = performance.now();
    desiredDay = state.day;
    setStorageUser(state.user_id);
    $("onboarding").hidden = !state.needs_timezone;
    $("app").hidden = state.needs_timezone;
    notice();
    if (state.needs_timezone) return;
    $("day").value = state.day;
    $("day").max = state.today;
    $("day").min = state.earliest_day;
    $("zone").textContent = state.timezone;
    renderDayLabel();
    const consumed = state.stats.calories;
    const over = Boolean(state.goal && consumed > state.goal);
    $("summary").classList.toggle("over", over);
    $("energy-label").textContent = state.goal ? (over ? "ПРЕВЫШЕНИЕ ЦЕЛИ" : "ОСТАЛОСЬ") : "ЭНЕРГИЯ ЗА ДЕНЬ";
    $("calories").textContent = whole(state.goal ? Math.abs(state.goal - consumed) : consumed);
    $("budget").textContent = state.goal ? `Съедено ${fmt(consumed)} из ${fmt(state.goal)} ккал` : "Задайте дневную цель, чтобы видеть остаток";
    renderTrack(consumed, over);
    let partial = false;
    for (const key of ["protein", "fat", "carbs"]) {
      const value = state.stats[key];
      const incomplete = value !== null && state.stats[`${key}_coverage`] < state.stats.entry_count;
      partial ||= incomplete;
      $(key).textContent = value === null ? "—" : `${incomplete ? "≈ " : ""}${fmt(value)} г`;
      $(key).classList.toggle("unknown", value === null);
    }
    $("partial").hidden = !partial;
    renderMacroSplit(partial);
    $("count").textContent = `${state.stats.entry_count} зап.`;
    if (!append) { $("entries").replaceChildren(); mealHead = null; }
    if (!state.stats.entry_count) $("entries").append(node("p", "Здесь пока пусто. Добавьте первый приём пищи — он появится в дневнике.", "empty"));
    state.entries.items.forEach(appendEntry);
    $("more").hidden = !state.entries.has_next;
    if (!append) { renderWeek(); loadWeek(); }
  } catch (error) {
    if (generation !== diaryGeneration) return;
    desiredDay = state?.day || "";
    if (state?.day) $("day").value = state.day;
    throw error;
  } finally {
    if (generation === diaryGeneration) {
    $("entries").setAttribute("aria-busy", "false");
    $("more").disabled = false;
    $("next").disabled = state?.day === state?.today;
    $("previous").disabled = state?.day === state?.earliest_day;
    }
  }
}
function openDialog(id) {
  const dialog = $(id);
  dialog.querySelectorAll(".form-error").forEach((el) => { el.textContent = ""; });
  dialog.showModal();
  dialog.querySelectorAll("form").forEach(form => baselines.set(form, formValues(form)));
  syncTelegram();
}
document.querySelectorAll(".close").forEach((button) => button.addEventListener("click", () => closeDialog(button.closest("dialog"))));
document.querySelectorAll("dialog").forEach(dialog => {
  dialog.addEventListener("cancel", event => { event.preventDefault(); closeDialog(dialog); });
  dialog.addEventListener("close", () => { syncTelegram(); renderDrafts(); });
});
function submit(id, handler) {
  $(id).addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (form.dataset.submitting) return;
    form.dataset.submitting = "true";
    syncTelegram();
    const buttons = [...form.querySelectorAll("button")];
    buttons.forEach((button) => { button.disabled = true; });
    const errorBox = form.querySelector(".form-error");
    if (errorBox) errorBox.textContent = "";
    saveDraft(form);
    const values = new FormData(form);
    const fields = [...form.querySelectorAll("input,select")].map(field => [field, field.disabled]);
    fields.forEach(([field]) => { field.disabled = true; });
    try { await handler(values); }
    catch (error) { if (errorBox && form.closest("dialog")?.open) errorBox.textContent = error.message; else notice(error.message); }
    finally {
      delete form.dataset.submitting;
      fields.forEach(([field, disabled]) => { field.disabled = disabled; });
      if (id === "weight-form") form.elements.measured_at.disabled = Boolean(editingWeight);
      buttons.forEach((button) => { button.disabled = false; }); syncTelegram();
    }
  });
}
async function added(dialogId, day = state.day) {
  const form = $(dialogId).querySelector("form");
  if (form) clearDraft(form.id);
  $(dialogId).close();
  tg?.HapticFeedback?.notificationOccurred("success");
  try { await loadDiary(day); } catch { notice("Запись сохранена. Не удалось обновить дневник — откройте приложение заново."); }
}
function localInput(epoch) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone: state.timezone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(new Date(epoch * 1000)).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}
function currentLocalTime() { return localInput(state.server_now + (performance.now() - receivedAt) / 1000); }
function defaultTime() { return state.day === currentLocalTime().slice(0, 10) ? currentLocalTime() : `${state.day}T12:00`; }
function foodUnit() {
  const form = $("food-form");
  const serving = form.elements.unit.value === "serving";
  $("amount-label").textContent = serving ? "Количество порций" : "Съедено, г";
  form.elements.grams.max = serving ? "1000" : "100000";
  form.elements.calories.max = serving ? "50000" : "10000";
  for (const key of ["protein", "fat", "carbs"]) form.elements[key].max = serving ? "10000" : "100";
}
function openFood(entry = null, template = false) {
  editingEntry = template ? null : entry;
  foodTemplate = template ? entry : null;
  const form = $("food-form");
  form.reset();
  form.elements.name.required = entry?.name !== null;
  form.elements.unit.disabled = Boolean(editingEntry);
  form.elements.unit.value = entry?.unit || "100g";
  form.elements.eaten_at.value = editingEntry ? localInput(entry.eaten_at_utc) : defaultTime();
  form.elements.eaten_at.min = editingEntry ? "" : state.earliest_entry_time;
  form.elements.eaten_at.max = currentLocalTime();
  $("food-timezone").textContent = `Часовой пояс: ${state.timezone}`;
  $("food-title").textContent = editingEntry ? "Редактировать запись" : "Добавить еду";
  $("food-save").textContent = editingEntry ? "Сохранить изменения" : "Добавить в дневник";
  if (entry) {
    form.elements.name.value = entry.name || "";
    form.elements.grams.value = entry.nutrition.servings ?? entry.nutrition.grams;
    for (const key of ["calories", "protein", "fat", "carbs"]) form.elements[key].value = entry.values[key] ?? "";
  }
  foodUnit();
  openDialog("food-dialog");
}
$("food-form").elements.unit.addEventListener("change", () => { foodUnit(); $("food-form").elements.grams.value = $("food-form").elements.unit.value === "serving" ? "1" : "100"; });
submit("timezone-form", async (form) => { await api("profile", "PUT", {timezone: form.get("timezone")}); await loadDiary(); });
submit("food-form", async (form) => {
  const data = {name: form.get("name"), eaten_at: form.get("eaten_at"), unit: $("food-form").elements.unit.value};
  for (const key of ["calories", "grams", "protein", "fat", "carbs"]) data[key] = form.get(key) === "" ? null : Number(form.get(key));
  if (data.unit === "serving") { data.amount = data.grams; delete data.grams; }
  if (data.unit === "serving" && foodTemplate?.nutrition.servings && foodTemplate.nutrition.grams != null) data.serving_grams = foodTemplate.nutrition.grams / foodTemplate.nutrition.servings;
  if (editingEntry) data.version = editingEntry.version;
  await api(editingEntry ? `entries/${editingEntry.entry_id}` : "entries", editingEntry ? "PATCH" : "POST", data);
  $("food-form").reset();
  await added("food-dialog", data.eaten_at.slice(0, 10));
});
submit("goal-form", async (form) => { await api("profile", "PUT", {goal: form.get("goal") === "" ? null : Number(form.get("goal"))}); $("goal-dialog").close(); await loadDiary(state.day); });
submit("favorite-form", async (form) => { await api("entries", "POST", {favorite_id: selectedFavorite.favorite_id, amount: Number(form.get("amount")), eaten_at: form.get("eaten_at")}); await added("favorites-dialog", form.get("eaten_at").slice(0, 10)); });
async function loadFavorites(append = false) {
  const generation = ++favoriteGeneration;
  const query = $("favorite-search").value.trim();
  $("favorite-status").textContent = "Загрузка…";
  let page;
  try { page = await api(`favorites?${new URLSearchParams({offset: String(append ? favoriteOffset : 0), q: query})}`); }
  catch (error) { if (generation === favoriteGeneration) $("favorite-status").textContent = error.message; return; }
  if (generation !== favoriteGeneration) return;
  $("favorite-status").textContent = "";
  if (!append) { $("favorites").replaceChildren(); $("favorite-form").hidden = true; }
  if (!page.items.length && !append) $("favorites").append(node("p", query ? "Ничего не найдено." : "Сохраните еду из дневника в избранное, чтобы добавлять её быстрее.", "empty"));
  for (const favorite of page.items) {
    const unit = favorite.unit === "serving" ? "порцию" : "100 г";
    const button = action(`${favorite.name} · ${fmt(favorite.calories_per_100g)} ккал на ${unit}`, () => {
      selectedFavorite = favorite;
      $("favorites").querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
      $("favorite-form").hidden = false;
      $("favorite-form").elements.eaten_at.value = defaultTime();
      $("favorite-form").elements.eaten_at.min = state.earliest_entry_time;
      $("favorite-form").elements.eaten_at.max = currentLocalTime();
      const input = $("favorite-form").elements.amount;
      $("favorite-amount-label").firstChild.textContent = favorite.unit === "serving" ? "Количество порций" : "Съедено, г";
      input.value = favorite.unit === "serving" ? "1" : "100";
      input.max = favorite.unit === "serving" ? "1000" : "100000";
      input.focus();
      baselines.set($("favorite-form"), formValues($("favorite-form")));
      syncTelegram();
    }, "");
    button.setAttribute("aria-pressed", "false");
    $("favorites").append(button);
  }
  favoriteOffset = page.offset + page.items.length;
  $("favorites-more").hidden = !page.has_next;
}
$("favorite-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  favoriteGeneration++;
  $("favorites").replaceChildren();
  $("favorite-form").hidden = true;
  $("favorites-more").hidden = true;
  searchTimer = setTimeout(() => loadFavorites(), 250);
});
function onClick(id, handler) {
  $(id).addEventListener("click", async () => {
    $(id).disabled = true;
    try { await handler(); } catch (error) { notice(error.message); }
    finally { $(id).disabled = false; if (id === "next") $(id).disabled = state?.day === state?.today; if (id === "previous") $(id).disabled = state?.day === state?.earliest_day; }
  });
}
onClick("add-button", () => openFood());
onClick("goal-button", () => { $("goal-form").elements.goal.value = state.goal ?? ""; openDialog("goal-dialog"); });
onClick("favorites-button", async () => { $("favorite-search").value = ""; openDialog("favorites-dialog"); await loadFavorites(); });
onClick("recent-button", async () => {
  const result = await api("recent");
  $("recent-list").replaceChildren();
  for (const entry of result.items) $("recent-list").append(action(entry.name, () => { $("recent-dialog").close(); openFood(entry, true); }, "wide"));
  if (!result.items.length) $("recent-list").append(node("p", "Здесь появится недавно записанная еда.", "empty"));
  openDialog("recent-dialog");
});
onClick("favorites-more", () => loadFavorites(true));
onClick("more", () => loadDiary(state.day, true));
onClick("today-button", () => loadDiary());
for (const [id, step] of [["previous", -1], ["next", 1]]) onClick(id, () => {
  const day = new Date(`${desiredDay || state.day}T12:00:00Z`);
  day.setUTCDate(day.getUTCDate() + step);
  return loadDiary(day.toISOString().slice(0, 10));
});
$("day").addEventListener("change", () => loadDiary($("day").value).catch((error) => notice(error.message)));
$("timezone-form").elements.timezone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Moscow";
function prettyDay(day) { return new Intl.DateTimeFormat("ru-RU", {day:"numeric",month:"short",timeZone:"UTC"}).format(new Date(`${day}T12:00:00Z`)); }
async function loadStats() {
  const generation = ++statsGeneration;
  $("stats-content").textContent = "Загрузка…";
  try {
    const result = await api(`statistics?${new URLSearchParams({day: state.day, period: $("stats-period").value})}`);
    if (generation !== statsGeneration) return;
    $("stats-range").textContent = `${prettyDay(result.start)} — ${prettyDay(result.end)} · ${state.timezone}`;
    const content = $("stats-content");
    content.replaceChildren(node("p", `${fmt(result.totals.calories)} ккал · ${result.totals.entry_count} записей`));
    content.append(node("p", result.logged_days ? `Среднее по ${result.logged_days} дням с записями: ${fmt(result.average_logged_day)} ккал` : "За этот период ещё нет записей.", "muted"));
    content.append(node("p", "День без записей означает отсутствие данных. Нажмите день, чтобы открыть дневник.", "small muted"));
    for (const key of ["protein", "fat", "carbs"]) {
      const value = result.totals[key];
      const incomplete = value != null && result.totals[key+"_coverage"] < result.totals.entry_count;
      content.append(node("p", `${{protein:"Белки",fat:"Жиры",carbs:"Углеводы"}[key]}: ${value == null ? "нет данных" : `${incomplete ? "≈ " : ""}${fmt(value)} г`}`, "small"));
    }
    const max = Math.max(1, ...result.days.map(day => day.calories || 0));
    for (const day of result.days) {
      const button = action(`${prettyDay(day.day)} · ${day.calories == null ? "нет записей" : `${fmt(day.calories)} ккал`}`, async () => { $("stats-dialog").close(); await loadDiary(day.day); }, "chart-row");
      if (day.calories != null) {
        const bar = document.createElement("progress"); bar.max=max; bar.value=day.calories; bar.className = state.goal && day.calories > state.goal ? "over-goal" : ""; bar.setAttribute("aria-label", `${prettyDay(day.day)}: ${fmt(day.calories)} ккал`); button.append(bar);
      }
      content.append(button);
    }
  } catch (error) { if (generation === statsGeneration) { $("stats-content").replaceChildren(node("p", error.message), action("Повторить", loadStats)); } }
}
function resetWeight() {
  editingWeight = null;
  const form = $("weight-form"); form.reset();
  form.elements.measured_at.disabled = false;
  form.elements.measured_at.value = defaultTime();
  form.elements.measured_at.min = state.earliest_entry_time;
  form.elements.measured_at.max = currentLocalTime();
  $("weight-save").textContent = "Записать вес";
  $("weight-cancel").hidden = true;
  baselines.set(form, formValues(form));
  syncTelegram();
}
async function loadWeights(append = false) {
  $("weight-status").textContent = "Загрузка…";
  try {
    const result = await api(`weights?${new URLSearchParams({day: state.day, offset: String(append ? weightOffset : 0)})}`);
    $("weight-status").textContent = "";
    const summary = [];
    if (result.latest) summary.push(`Последнее: ${fmt(result.latest.weight_kg)} кг (${prettyDay(localInput(result.latest.measured_at_utc).slice(0,10))})`);
    if (result.average != null) summary.push(`Среднее за 7 дней: ${fmt(result.average)} кг`);
    if (result.average != null && result.previous_average != null) { const change = result.average-result.previous_average; summary.push(`К предыдущим 7 дням: ${change > 0 ? "+" : ""}${fmt(change)} кг`); }
    $("weights-summary").textContent = `На ${prettyDay(state.day)} · ${state.timezone}. ${summary.join(". ") || "Измерений пока нет."}`;
    if (!append) $("weight-list").replaceChildren();
    for (const record of result.items) {
      const row = node("div", "", "entry");
      row.append(node("p", `${localInput(record.measured_at_utc).replace("T"," ")} · ${fmt(record.weight_kg)} кг`));
      row.append(action("Изменить", async () => {
        if (dirty($("weight-form")) && !await confirmAction("Заменить несохранённые данные формы?")) return;
        editingWeight=record;
        const form=$("weight-form"); form.elements.weight_kg.value=record.weight_kg; form.elements.measured_at.value=localInput(record.measured_at_utc); form.elements.measured_at.disabled=true;
        $("weight-save").textContent="Сохранить вес"; $("weight-cancel").hidden=false;
        baselines.set(form, formValues(form)); syncTelegram(); form.elements.weight_kg.focus();
      }), action("Удалить", async () => {
        if (!await confirmAction(`Удалить измерение ${fmt(record.weight_kg)} кг?`)) return;
        await api(`weights/${record.weight_id}`, "DELETE", {version:record.version});
        if (editingWeight?.weight_id === record.weight_id) resetWeight();
        await loadWeights();
      }));
      $("weight-list").append(row);
    }
    weightOffset=result.offset+result.items.length;
    $("weights-more").hidden=!result.has_next;
    $("weight-chart").replaceChildren();
    const max = Math.max(1, ...result.days.map(day => day.weight_kg));
    for (const day of result.days) {
      const row=node("div", `${prettyDay(day.day)} · ${fmt(day.weight_kg)} кг`, "chart-row");
      const bar=document.createElement("progress");bar.max=max;bar.value=day.weight_kg;bar.setAttribute("aria-label",`${prettyDay(day.day)}: ${fmt(day.weight_kg)} кг`);row.append(bar);$("weight-chart").append(row);
    }
    if (!result.days.length) $("weight-chart").textContent="Нет измерений за эти 30 дней.";
  } catch (error) { $("weight-status").replaceChildren(node("span",error.message),action("Повторить",()=>loadWeights(append))); }
}
submit("weight-form", async form => {
  const data={weight_kg:Number(form.get("weight_kg"))};
  if (editingWeight) data.version=editingWeight.version; else data.measured_at=form.get("measured_at");
  await api(editingWeight ? `weights/${editingWeight.weight_id}` : "weights", editingWeight ? "PATCH" : "POST", data);
  clearDraft("weight-form"); resetWeight(); await loadWeights();
});
onClick("stats-button", async () => {openDialog("stats-dialog"); await loadStats();});
$("stats-period").addEventListener("change", loadStats);
onClick("weights-button", async () => {resetWeight();openDialog("weights-dialog");await loadWeights();});
onClick("weights-more", () => loadWeights(true));
onClick("weight-cancel", resetWeight);
function supports(version) { return Boolean(tg?.initData && tg.isVersionAtLeast?.(version)); }
function formValues(form) {
  return JSON.stringify(Object.fromEntries([...form.elements].filter(el => el.name).map(el => [el.name, el.value])));
}
function activeForm() { return [...document.querySelectorAll("dialog[open] form")].find(form => !form.hidden); }
function dirty(form) { return Boolean(form && baselines.has(form) && baselines.get(form) !== formValues(form)); }
function clearDraft(id) {
  clearTimeout(draftTimer);
  delete drafts[id];
  saveStorage(`kcalorie-drafts-${storageUser}`, drafts);
  renderDrafts();
}
const draftNames = {"food-form": "Еда", "favorite-form": "Избранное", "weight-form": "Вес"};
function saveDraft(form) {
  if (!storageUser || !draftNames[form.id] || !dirty(form)) return;
  drafts[form.id] = {values: JSON.parse(formValues(form)), editingEntry, foodTemplate, selectedFavorite, editingWeight, timezone: state.timezone};
  saveStorage(`kcalorie-drafts-${storageUser}`, drafts);
  renderDrafts();
}
function renderDrafts() {
  $("drafts").replaceChildren();
  for (const [id, draft] of Object.entries(drafts)) {
    if (!draftNames[id] || !draft?.values) continue;
    if ($(id).closest("dialog")?.open) continue;
    const row = node("div", "", "draft-row");
    row.append(node("p", `${draftNames[id]} · ${(draft.values.eaten_at || draft.values.measured_at)?.replace("T", " ") || "черновик"}`));
    row.append(action("Продолжить", () => {
      if (draft.timezone !== state.timezone) { notice("У черновика другой часовой пояс. Верните прежний пояс в боте или удалите черновик."); return; }
      if (id === "food-form") openFood(draft.editingEntry || draft.foodTemplate, !draft.editingEntry && Boolean(draft.foodTemplate));
      else {
        if (id === "weight-form") {
          resetWeight(); editingWeight = draft.editingWeight;
          $("weight-form").elements.measured_at.disabled = Boolean(editingWeight);
          $("weight-save").textContent = editingWeight ? "Сохранить вес" : "Записать вес";
          $("weight-cancel").hidden = !editingWeight;
          loadWeights();
        }
        if (id === "favorite-form") { selectedFavorite = draft.selectedFavorite; $("favorite-form").hidden = false; $("favorites").replaceChildren(node("p", selectedFavorite.name)); $("favorites-more").hidden = true; }
        openDialog($(id).closest("dialog").id);
      }
      const form = $(id);
      for (const [key, value] of Object.entries(draft.values)) if (form.elements.namedItem(key)) form.elements.namedItem(key).value = value;
      if (form.elements.eaten_at) form.elements.eaten_at.max = currentLocalTime();
      if (id === "food-form") foodUnit();
      baselines.set(form, "");
      syncTelegram();
    }), action("Убрать", () => clearDraft(id)));
    $("drafts").append(row);
  }
  $("drafts").hidden = !$("drafts").childElementCount;
}
function confirmAction(message) {
  if (supports("6.2") && tg.showConfirm) return new Promise(resolve => tg.showConfirm(message, resolve));
  return Promise.resolve(window.confirm(message));
}
async function closeDialog(dialog) {
  if (!dialog || dialog.dataset.closing) return;
  const form = dialog.querySelector("form:not([hidden])");
  if (form?.dataset.submitting) return;
  dialog.dataset.closing = "true";
  try {
    if (dirty(form)) {
      saveDraft(form);
      if (!await confirmAction(draftNames[form.id] ? "Закрыть форму? Черновик останется на этом устройстве." : "Закрыть без сохранения изменений?")) return;
    }
    dialog.close();
  } finally { delete dialog.dataset.closing; }
}
function syncTelegram() {
  const dialog = document.querySelector("dialog[open]");
  const form = activeForm();
  document.documentElement.classList.toggle("locked", Boolean(dialog));
  if (supports("6.1") && tg.BackButton?.isVisible !== Boolean(dialog)) {
    if (dialog) tg.BackButton?.show(); else tg.BackButton?.hide();
  }
  if (supports("6.2")) {
    const wanted = Boolean(dirty(form) || form?.dataset.submitting);
    if (wanted !== confirmingClose) {
      confirmingClose = wanted;
      if (wanted) tg.enableClosingConfirmation?.(); else tg.disableClosingConfirmation?.();
    }
  }
  if (tg?.initData && tg.MainButton) {
    const main = tg.MainButton;
    const button = form?.querySelector("button:not([type=button])");
    if (button) {
      // MainButton submits this form, so the form's own button would duplicate it.
      button.hidden = true;
      if (main.text !== button.textContent) main.setText(button.textContent);
      if (main.isVisible !== true) main.show();
      const busy = Boolean(form.dataset.submitting);
      if (main.isProgressVisible !== busy) { if (busy) main.showProgress(); else main.hideProgress(); }
      if (main.isActive === busy) { if (busy) main.disable(); else main.enable(); }
    } else if (main.isVisible !== false) {
      main.hideProgress();
      main.hide();
    }
  }
}
document.querySelectorAll("form").forEach(form => form.addEventListener("input", () => {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => saveDraft(form), 400);
  syncTelegram();
}));
window.addEventListener("beforeunload", event => {
  const form = activeForm();
  if (dirty(form) || form?.dataset.submitting) { saveDraft(form); event.preventDefault(); event.returnValue = ""; }
});
function applyTheme() {
  if (tg?.initData) {
    document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
    for (const name of [...document.documentElement.style]) if (name.startsWith("--tg-theme-")) document.documentElement.style.removeProperty(name);
    for (const [key, value] of Object.entries(tg.themeParams || {})) {
      if (/^#[0-9a-f]{6}$/i.test(value)) document.documentElement.style.setProperty(`--tg-theme-${key.replaceAll("_", "-")}`, value);
    }
    if (supports("6.1")) { tg.setHeaderColor?.("secondary_bg_color"); tg.setBackgroundColor?.("secondary_bg_color"); }
  }
}
function resizeViewport() {
  const height = Math.min(window.visualViewport?.height || innerHeight, tg?.viewportStableHeight || innerHeight);
  document.documentElement.style.setProperty("--visible-height", `${height}px`);
  document.documentElement.style.setProperty("--visible-top", `${window.visualViewport?.offsetTop || 0}px`);
}
if (tg?.initData) {
  tg.onEvent?.("themeChanged", applyTheme);
  tg.onEvent?.("viewportChanged", resizeViewport);
  if (supports("6.1")) tg.BackButton?.onClick(() => closeDialog(document.querySelector("dialog[open]")));
  tg.MainButton?.onClick(() => { const form = activeForm(); if (!form?.dataset.submitting) form?.requestSubmit(); });
  if (supports("7.7")) tg.disableVerticalSwipes?.();
}
window.visualViewport?.addEventListener("resize", resizeViewport);
window.visualViewport?.addEventListener("scroll", resizeViewport);
window.addEventListener("resize", resizeViewport);
applyTheme();
resizeViewport();
tg?.ready();
tg?.expand();
if (!tg?.initData) notice("Откройте дневник кнопкой в Telegram-боте: команда /app.");
else loadDiary().catch((error) => notice(error.message));
