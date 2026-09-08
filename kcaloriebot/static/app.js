"use strict";
const tg = window.Telegram?.WebApp;
const $ = (id) => document.getElementById(id);
const fmt = (value) => new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 1}).format(value);
let state, selectedFavorite, selectedEntry, favoriteOffset = 0, loading = false;

async function api(path, method = "GET", data) {
  let response;
  try {
    response = await fetch(`/api/${path}`, {
      method,
      headers: {Authorization: `tma ${tg?.initData || ""}`, "Content-Type": "application/json"},
      body: data === undefined ? undefined : JSON.stringify(data),
    });
  } catch {
    throw new Error("Нет соединения. Проверьте интернет. Если вы добавляли еду, обновите дневник перед повтором.");
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Не удалось выполнить запрос.");
  return result;
}
function notice(text = "") { $("notice").textContent = text; $("notice").hidden = !text; }
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
function renderEntry(entry) {
  const card = node("article", "", "entry");
  const row = node("div", "", "row");
  row.append(node("span", entry.name || "Без названия", "entry-name"), node("strong", `${fmt(entry.nutrition.calories)} ккал`));
  const time = new Intl.DateTimeFormat("ru-RU", {hour: "2-digit", minute: "2-digit", timeZone: state.timezone}).format(new Date(entry.eaten_at_utc * 1000));
  const amount = entry.nutrition.servings == null ? `${fmt(entry.nutrition.grams)} г` : `${fmt(entry.nutrition.servings)} порц.`;
  card.append(row, node("p", `${time} · ${amount}`, "muted small"));
  const controls = node("div", "", "entry-actions");
  if (entry.name) controls.append(action("♡ В избранное", async () => { await api(`entries/${entry.entry_id}/favorite`, "POST", {}); notice("Сохранено в избранное."); }));
  controls.append(action("Удалить", () => { selectedEntry = entry; $("delete-name").textContent = entry.name || "Без названия"; openDialog("delete-dialog"); }));
  card.append(controls);
  return card;
}
async function loadDiary(day = "", append = false) {
  if (loading) return;
  loading = true;
  for (const id of ["previous", "next", "day", "more", "today-button"]) $(id).disabled = true;
  try {
    const offset = append ? state.entries.offset + state.entries.items.length : 0;
    const query = new URLSearchParams({offset: String(offset)});
    if (day) query.set("day", day);
    state = await api(`diary?${query}`);
    $("onboarding").hidden = !state.needs_timezone;
    $("app").hidden = state.needs_timezone;
    notice();
    if (state.needs_timezone) return;
    $("day").value = state.day;
    $("day").max = state.today;
    $("zone").textContent = state.timezone;
    $("calories").textContent = fmt(state.stats.calories);
    $("progress").hidden = !state.goal;
    $("progress").value = state.goal ? Math.min(100, state.stats.calories / state.goal * 100) : 0;
    const left = state.goal - state.stats.calories;
    $("budget").textContent = state.goal ? (left >= 0 ? `Осталось ${fmt(left)} из ${fmt(state.goal)} ккал` : `Выше цели на ${fmt(-left)} ккал`) : "Задайте дневную цель для отслеживания прогресса";
    let partial = false;
    for (const key of ["protein", "fat", "carbs"]) {
      const value = state.stats[key];
      const incomplete = value !== null && state.stats[`${key}_coverage`] < state.stats.entry_count;
      partial ||= incomplete;
      $(key).textContent = value === null ? "—" : `${incomplete ? "≈ " : ""}${fmt(value)} г`;
    }
    $("partial").hidden = !partial;
    $("count").textContent = `${state.stats.entry_count} зап.`;
    if (!append) $("entries").replaceChildren();
    if (!state.stats.entry_count) $("entries").append(node("p", "Здесь пока пусто. Добавьте первый приём пищи — он появится в дневнике.", "empty"));
    state.entries.items.forEach((entry) => $("entries").append(renderEntry(entry)));
    $("more").hidden = !state.entries.has_next;
  } finally {
    loading = false;
    for (const id of ["previous", "next", "day", "more", "today-button"]) $(id).disabled = false;
    $("next").disabled = state?.day === state?.today;
    $("previous").disabled = state?.day === "2020-01-01";
  }
}
function openDialog(id) {
  const dialog = $(id);
  dialog.querySelectorAll(".form-error").forEach((el) => { el.textContent = ""; });
  dialog.showModal();
}
document.querySelectorAll(".close").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
function submit(id, handler) {
  $(id).addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const buttons = [...form.querySelectorAll("button")];
    buttons.forEach((button) => { button.disabled = true; });
    const errorBox = form.querySelector(".form-error");
    if (errorBox) errorBox.textContent = "";
    try { await handler(new FormData(form)); }
    catch (error) { if (errorBox && form.closest("dialog")?.open) errorBox.textContent = error.message; else notice(error.message); }
    finally { buttons.forEach((button) => { button.disabled = false; }); }
  });
}
async function added(dialogId) {
  $(dialogId).close();
  tg?.HapticFeedback?.notificationOccurred("success");
  try { await loadDiary(); } catch { notice("Запись сохранена. Не удалось обновить дневник — откройте приложение заново."); }
}
submit("timezone-form", async (form) => { await api("profile", "PUT", {timezone: form.get("timezone")}); await loadDiary(); });
submit("food-form", async (form) => {
  const data = {name: form.get("name")};
  for (const key of ["calories", "grams", "protein", "fat", "carbs"]) data[key] = form.get(key) === "" ? null : Number(form.get(key));
  await api("entries", "POST", data);
  $("food-form").reset();
  await added("food-dialog");
});
submit("goal-form", async (form) => { await api("profile", "PUT", {goal: form.get("goal") === "" ? null : Number(form.get("goal"))}); $("goal-dialog").close(); await loadDiary(state.day); });
submit("favorite-form", async (form) => { await api("entries", "POST", {favorite_id: selectedFavorite.favorite_id, amount: Number(form.get("amount"))}); await added("favorites-dialog"); });
submit("delete-form", async () => { await api(`entries/${selectedEntry.entry_id}`, "DELETE"); $("delete-dialog").close(); await loadDiary(state.day); });
async function loadFavorites(append = false) {
  const page = await api(`favorites?offset=${append ? favoriteOffset : 0}`);
  if (!append) { $("favorites").replaceChildren(); $("favorite-form").hidden = true; }
  if (!page.items.length && !append) $("favorites").append(node("p", "Сохраните еду из дневника в избранное, чтобы добавлять её быстрее.", "empty"));
  for (const favorite of page.items) {
    const unit = favorite.unit === "serving" ? "порцию" : "100 г";
    const button = action(`${favorite.name} · ${fmt(favorite.calories_per_100g)} ккал на ${unit}`, () => {
      selectedFavorite = favorite;
      $("favorites").querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
      $("favorite-form").hidden = false;
      const input = $("favorite-form").elements.amount;
      $("favorite-amount-label").firstChild.textContent = favorite.unit === "serving" ? "Количество порций" : "Съедено, г";
      input.value = favorite.unit === "serving" ? "1" : "100";
      input.max = favorite.unit === "serving" ? "1000" : "100000";
      input.focus();
    }, "");
    button.setAttribute("aria-pressed", "false");
    $("favorites").append(button);
  }
  favoriteOffset = page.offset + page.items.length;
  $("favorites-more").hidden = !page.has_next;
}
function onClick(id, handler) {
  $(id).addEventListener("click", async () => {
    $(id).disabled = true;
    try { await handler(); } catch (error) { notice(error.message); }
    finally { $(id).disabled = false; if (id === "next") $(id).disabled = state?.day === state?.today; if (id === "previous") $(id).disabled = state?.day === "2020-01-01"; }
  });
}
onClick("add-button", () => openDialog("food-dialog"));
onClick("goal-button", () => { $("goal-form").elements.goal.value = state.goal ?? ""; openDialog("goal-dialog"); });
onClick("favorites-button", async () => { await loadFavorites(); openDialog("favorites-dialog"); });
onClick("favorites-more", () => loadFavorites(true));
onClick("more", () => loadDiary(state.day, true));
onClick("today-button", () => loadDiary());
for (const [id, step] of [["previous", -1], ["next", 1]]) onClick(id, () => {
  const day = new Date(`${state.day}T12:00:00Z`);
  day.setUTCDate(day.getUTCDate() + step);
  return loadDiary(day.toISOString().slice(0, 10));
});
$("day").addEventListener("change", () => loadDiary($("day").value).catch((error) => notice(error.message)));
$("timezone-form").elements.timezone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Moscow";
tg?.ready();
tg?.expand();
if (!tg?.initData) notice("Откройте дневник кнопкой в Telegram-боте: команда /app.");
else loadDiary().catch((error) => notice(error.message));
