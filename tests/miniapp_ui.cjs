const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium, webkit} = require(process.env.PLAYWRIGHT_MODULE_PATH || path.resolve('data/ui-check/node_modules/playwright'));

(async () => {
  const safari = process.env.MINIAPP_BROWSER === 'webkit';
  const browser = await (safari ? webkit : chromium).launch({headless: true, ...(!safari && process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 320, height: 740}, colorScheme: 'light', isMobile: safari, hasTouch: safari});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://telegram.org/**', route => route.fulfill({contentType: 'text/javascript', body: `
      const events = {};
      const main = {isVisible:false,isActive:true,isProgressVisible:false,showCount:0,
        setText(text){this.text=text;return this},show(){this.isVisible=true;this.showCount++;return this},hide(){this.isVisible=false},
        disable(){this.isActive=false},enable(){this.isActive=true},showProgress(){this.isProgressVisible=true},hideProgress(){this.isProgressVisible=false},onClick(fn){this.click=fn}};
      window.Telegram = {WebApp: {initData: ${JSON.stringify(process.argv[3])}, colorScheme:'dark', themeParams:{},
        isVersionAtLeast:()=>true, ready(){},expand(){},disableVerticalSwipes(){},setHeaderColor(){},setBackgroundColor(){},
        onEvent(name,fn){events[name]=fn}, emit(name){events[name]?.()},
        BackButton:{show(){},hide(){},onClick(fn){this.click=fn}},MainButton:main,
        enableClosingConfirmation(){this.dirty=true},disableClosingConfirmation(){this.dirty=false},showConfirm(text,fn){fn(true)}
      }};`
    }));
    await page.goto(process.argv[2]);
    await page.locator('#app').waitFor({state:'visible'});
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    // Anything under 16px makes mobile WebViews zoom into the field on focus.
    assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll('input,select,textarea')]
      .filter(el => parseFloat(getComputedStyle(el).fontSize) < 16).map(el => el.name || el.id)), []);
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark');
    await page.locator('#previous').click();
    await page.waitForFunction(() => !document.getElementById('next').disabled);
    const day = await page.locator('#day').inputValue();
    await page.locator('#add-button').click();
    assert.equal(await page.locator('#food-form [name=eaten_at]').inputValue(), day+'T12:00');
    // Telegram's MainButton is the only submit control while the sheet is open.
    assert.ok(await page.locator('#food-save').isHidden());
    // Opening a sheet must not focus a field: iOS zooms to the focused control.
    assert.ok(await page.evaluate(() => !['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)));
    assert.ok(await page.locator('#food-dialog .close').evaluate(el => el === document.activeElement));
    const mainShows = await page.evaluate(() => Telegram.WebApp.MainButton.showCount);
    const inputScale = await page.evaluate(() => visualViewport.scale);
    await page.locator('#food-form [name=name]').fill('овсянка');
    await page.locator('#food-form [name=calories]').fill('370');
    await page.locator('#food-form [name=protein]').fill('12');
    await page.locator('#food-form [name=grams]').fill('60');
    assert.equal(await page.evaluate(() => Telegram.WebApp.MainButton.showCount), mainShows);
    assert.equal(await page.evaluate(() => visualViewport.scale), inputScale);
    await page.evaluate(() => Telegram.WebApp.BackButton.click());
    await page.locator('#food-dialog').waitFor({state:'hidden'});
    await page.reload();
    await page.locator('#drafts').waitFor({state:'visible'});
    await page.locator('#drafts').getByRole('button', {name:'Продолжить'}).click();
    assert.equal(await page.locator('#food-form [name=eaten_at]').inputValue(), day+'T12:00');
    // Commit the first request, then lose its response. Retrying must replay it.
    let lost = false;
    await page.route('**/api/entries', async route => {
      if (route.request().method()==='POST' && !lost) { lost=true; await route.fetch(); await route.abort(); }
      else await route.continue();
    });
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    await page.locator('#food-dialog').waitFor({state:'hidden'});
    await page.waitForFunction(() => document.getElementById('calories').textContent === '222');
    assert.equal(await page.locator('#entries .entry').count(), 1);
    // Known protein with unknown fat/carbs must not look like 100% protein.
    assert.ok(await page.locator('#macro-split').isHidden());
    assert.equal(await page.locator('#entries .meal-head').count(), 1);
    assert.equal(await page.locator('#week .week-day').count(), 7);
    assert.equal(await page.locator('#day').inputValue(), day);
    await page.getByRole('button', {name:'Редактировать: овсянка',exact:true}).click();
    await page.locator('#food-form [name=grams]').fill('100');
    await page.locator('#food-form [name=fat]').fill('0');
    await page.locator('#food-form [name=carbs]').fill('0');
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    await page.waitForFunction(() => document.getElementById('calories').textContent === '370');
    assert.ok(await page.locator('#macro-split').isVisible());
    await page.getByRole('button', {name:'Удалить',exact:true}).click();
    await page.waitForFunction(() => document.getElementById('calories').textContent === '0');
    await page.getByRole('button', {name:'Отменить',exact:true}).click();
    await page.waitForFunction(() => document.getElementById('calories').textContent === '370');
    await page.locator('#recent-button').click();
    await page.locator('#recent-list button').click();
    assert.equal(await page.locator('#food-form [name=grams]').inputValue(), '100');
    assert.equal(await page.locator('#food-form [name=eaten_at]').inputValue(), day+'T12:00');
    await page.locator('#food-dialog .close').click();
    await page.getByRole('button',{name:'♡ В избранное',exact:true}).click();
    await page.locator('#favorites-button').click();
    await page.locator('#favorite-search').fill('ОВСЯ');
    await page.locator('#favorites button').waitFor();
    assert.equal(await page.locator('#favorites button').count(),1);
    await page.locator('#favorites button').click();
    assert.equal(await page.locator('#favorite-form [name=eaten_at]').inputValue(),day+'T12:00');
    await page.locator('#favorites-dialog .close').click();
    await page.locator('#favorites-dialog').waitFor({state:'hidden'});
    // Product search: the external database is stubbed; favorites are real.
    const searches = [];
    await page.route('**/api/food-search?*', route => {
      searches.push(new URL(route.request().url()).searchParams.get('q'));
      return route.fulfill({contentType:'application/json', body: JSON.stringify({page:1, has_next:false, items:[
        {source:'openfoodfacts', code:'1', name:'Гречка (Мистраль)', title:'Гречка', brand:'Мистраль', calories:351, protein:12, fat:3.4, carbs:null}]})});
    });
    await page.locator('#search-button').click();
    assert.ok(await page.evaluate(() => !Telegram.WebApp.MainButton.isVisible));
    await page.locator('#product-search').fill('гречка');
    await page.locator('#product-search').press('Enter');
    await page.locator('#products .product').waitFor();
    assert.deepEqual(searches, ['гречка']);
    assert.match(await page.locator('#products .product-values').textContent(), /351 ккал · Б 12 · Ж 3,4 · У — на 100 г/);
    await page.getByRole('button', {name:'В избранное', exact:true}).click();
    await page.locator('#products .product-saved').waitFor();
    await page.locator('#products .product-main').click();
    await page.locator('#search-dialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('#food-form [name=name]').inputValue(), 'Гречка (Мистраль)');
    assert.equal(await page.locator('#food-form [name=calories]').inputValue(), '351');
    assert.equal(await page.locator('#food-form [name=carbs]').inputValue(), '');
    assert.equal(await page.locator('#food-form [name=grams]').inputValue(), '100');
    assert.equal(await page.locator('#food-form [name=eaten_at]').inputValue(), day+'T12:00');
    await page.locator('#food-dialog .close').click();
    await page.locator('#food-dialog').waitFor({state:'hidden'});
    await page.locator('#favorites-button').click();
    await page.locator('#favorite-search').fill('мистраль');
    await page.locator('#favorites button').waitFor();
    assert.match(await page.locator('#favorites button').textContent(), /Гречка \(Мистраль\) · 351 ккал на 100 г/);
    await page.locator('#favorites-dialog .close').click();
    await page.locator('#favorites-dialog').waitFor({state:'hidden'});
    // Open from a scrolled diary. The fixed background must retain its geometry.
    await page.locator('#add-button').scrollIntoViewIfNeeded();
    const background = await page.locator('#summary').boundingBox();
    const scrollBefore = await page.evaluate(() => scrollY);
    await page.locator('#add-button').click();
    const backgroundLocked = await page.locator('#summary').boundingBox();
    assert.ok(Math.abs(background.width-backgroundLocked.width)<1);
    assert.ok(Math.abs(background.y-backgroundLocked.y)<1);
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).position), 'fixed');
    // Telegram's previous stable height must not override the current viewport.
    await page.evaluate(() => {Telegram.WebApp.viewportStableHeight=200;Telegram.WebApp.emit('viewportChanged')});
    assert.equal(await page.evaluate(() => parseFloat(document.documentElement.style.getPropertyValue('--visible-height'))), await page.evaluate(() => visualViewport.height));
    await page.evaluate(() => {delete Telegram.WebApp.viewportStableHeight;Telegram.WebApp.emit('viewportChanged')});
    await page.setViewportSize({width:320,height:360});
    await page.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--visible-height').trim() === '360px');
    await page.locator('#food-dialog').evaluate(el => Promise.all(el.getAnimations().map(animation => animation.finished)));
    const box=await page.locator('#food-dialog').boundingBox();
    assert.ok(box.y >= 0 && box.y + box.height <= 361, JSON.stringify(box));
    assert.ok(await page.locator('#food-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth));
    // With a short keyboard-sized viewport, gestures may scroll the form but
    // must not pan the page sideways, on the backdrop, or past either edge.
    const gestures = await page.locator('#food-dialog').evaluate(sheet => {
      const field = sheet.querySelector('[name=name]');
      field.focus({preventScroll:true});
      function drag(target, x, y, dx, dy) {
        function send(type, point) {
          const event = new Event(type, {bubbles:true, cancelable:true});
          Object.defineProperty(event, 'touches', {value:point ? [point] : []});
          target.dispatchEvent(event);
          return event.defaultPrevented;
        }
        send('touchstart', {clientX:x, clientY:y});
        const blocked = send('touchmove', {clientX:x+dx, clientY:y+dy});
        send('touchend');
        return blocked;
      }
      const box = sheet.getBoundingClientRect();
      const x = box.left+box.width/2, y = box.top+box.height/2;
      sheet.scrollTop = 0;
      const top = drag(field,x,y,0,30);
      const down = drag(field,x,y,0,-30);
      sheet.scrollTop = (sheet.scrollHeight-sheet.clientHeight)/2;
      const middleUp = drag(field,x,y,0,30);
      const middleDown = drag(field,x,y,0,-30);
      const right = drag(field,x,y,30,0);
      const left = drag(field,x,y,-30,0);
      sheet.scrollTop = sheet.scrollHeight;
      const bottom = drag(field,x,y,0,-30);
      const backdrop = drag(sheet,box.left+2,box.top-5,0,-30);
      sheet.scrollTop = 0;
      field.blur();
      return {top,down,middleUp,middleDown,right,left,bottom,backdrop};
    });
    assert.deepEqual(gestures, {top:true,down:false,middleUp:false,middleDown:false,right:true,left:true,bottom:true,backdrop:true});
    // The page behind an open sheet is frozen, so the keyboard cannot scroll it.
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).overflow), 'hidden');
    await page.locator('#food-dialog .close').click();
    await page.locator('#food-dialog').waitFor({state:'hidden'});
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).overflow), 'visible');
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).position), 'static');
    await page.setViewportSize({width:320,height:740});
    // The shorter viewport may clamp scroll, but opening/closing again must not drift.
    await page.evaluate(y => scrollTo(0, y), scrollBefore);
    await page.locator('#add-button').scrollIntoViewIfNeeded();
    const restoredScroll = await page.evaluate(() => scrollY);
    await page.locator('#add-button').click();
    await page.locator('#food-dialog .close').click();
    await page.locator('#food-dialog').waitFor({state:'hidden'});
    assert.equal(await page.evaluate(() => scrollY), restoredScroll);
    if(process.env.MINIAPP_SCREENSHOTS) await page.screenshot({path:path.join(process.env.MINIAPP_SCREENSHOTS,'miniapp-updated-dark.png'),fullPage:true});
    await page.evaluate(() => {Telegram.WebApp.colorScheme='light';Telegram.WebApp.emit('themeChanged')});
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'light');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    if(process.env.MINIAPP_SCREENSHOTS) await page.screenshot({path:path.join(process.env.MINIAPP_SCREENSHOTS,'miniapp-updated-light.png'),fullPage:true});
    await page.locator('#stats-button').click();
    await page.locator('#stats-content .chart-row').last().waitFor();
    assert.equal(await page.locator('#stats-content .chart-row').count(),7);
    await page.locator('#stats-dialog .close').click();
    await page.locator('#weights-button').click();
    await page.locator('#weight-form [name=weight_kg]').fill('80');
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    await page.locator('#weight-list .entry').waitFor();
    assert.equal(await page.locator('#weight-list .entry').count(),1);
    await page.locator('#weight-list').getByRole('button',{name:'Изменить',exact:true}).click();
    await page.locator('#weight-form [name=weight_kg]').fill('79');
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    await page.waitForFunction(() => document.getElementById('weight-list').textContent.includes('79 кг'));
    await page.locator('#weight-list').getByRole('button',{name:'Удалить',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('#weight-list .entry'));
    await page.locator('#weights-dialog .close').click();
    // Inline favorites: reversed fragments, explicit selection, preserved date
    // and quantity, then a serving-based favorite restored from a draft.
    await page.locator('#weights-dialog').waitFor({state:'hidden'});
    await page.locator('#add-button').click();
    const foodName = page.locator('#food-form [name=name]');
    const mealTime = await page.locator('#food-form [name=eaten_at]').inputValue();
    await page.locator('#food-form [name=grams]').fill('55');
    await foodName.fill('КУР БЕД');
    const chicken = page.locator('#food-suggestions-list button').filter({hasText:'Бедро куриное'});
    await chicken.waitFor();
    assert.equal(await page.locator('#food-form [name=calories]').inputValue(), '');
    await chicken.click();
    assert.equal(await foodName.inputValue(), 'Бедро куриное');
    assert.equal(await page.locator('#food-form [name=calories]').inputValue(), '170');
    assert.equal(await page.locator('#food-form [name=carbs]').inputValue(), '');
    assert.equal(await page.locator('#food-form [name=grams]').inputValue(), '55');
    assert.equal(await page.locator('#food-form [name=eaten_at]').inputValue(), mealTime);
    assert.ok(await page.locator('#food-suggestions').isHidden());
    // A stale response must not resurrect suggestions after the text is cleared.
    let releaseSearch, searchStarted, searchFinished;
    const started = new Promise(resolve => {searchStarted=resolve});
    const release = new Promise(resolve => {releaseSearch=resolve});
    const finished = new Promise(resolve => {searchFinished=resolve});
    await page.route('**/api/favorites?*', async route => {
      const response = await route.fetch();
      searchStarted();
      await release;
      await route.fulfill({response});
      searchFinished();
    });
    await foodName.fill('кур');
    await started;
    await foodName.fill('');
    releaseSearch();
    await finished;
    await page.unroute('**/api/favorites?*');
    assert.ok(await page.locator('#food-suggestions').isHidden());
    await foodName.fill('неизвестная еда');
    await page.waitForFunction(() => document.getElementById('food-suggestions-status').textContent.includes('не найдено'));
    assert.equal(await page.locator('#food-form [name=calories]').inputValue(), '170');
    await foodName.fill('орех бат');
    await page.locator('#food-suggestions-list button').filter({hasText:'Батончик ореховый'}).click();
    assert.equal(await page.locator('#food-form [name=unit]').inputValue(), 'serving');
    assert.equal(await page.locator('#food-form [name=grams]').inputValue(), '1');
    assert.equal(await page.locator('#food-form [name=calories]').inputValue(), '200');
    await page.locator('#food-dialog .close').click();
    await page.locator('#food-dialog').waitFor({state:'hidden'});
    await page.reload();
    await page.locator('#drafts').getByRole('button', {name:'Продолжить'}).click();
    assert.equal(await foodName.inputValue(), 'Батончик ореховый');
    assert.equal(await page.locator('#food-form [name=eaten_at]').inputValue(), mealTime);
    const saved = page.waitForResponse(response => response.url().endsWith('/api/entries') && response.request().method() === 'POST');
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    const savedEntry = await (await saved).json();
    assert.equal(savedEntry.nutrition.calories, 200);
    assert.equal(savedEntry.nutrition.grams, 50);
    assert.equal(savedEntry.nutrition.servings, 1);
    await page.locator('#food-dialog').waitFor({state:'hidden'});
    // Restoring a gram draft after selecting a serving restores its limits too.
    await page.locator('#favorites-button').click();
    await page.locator('#favorites button').filter({hasText:'Бедро куриное'}).click();
    await page.locator('#favorite-form [name=amount]').fill('2000');
    await page.locator('#favorites-dialog .close').click();
    await page.locator('#favorites-dialog').waitFor({state:'hidden'});
    await page.locator('#favorites-button').click();
    await page.locator('#favorites button').filter({hasText:'Батончик ореховый'}).click();
    await page.locator('#favorites-dialog .close').click();
    await page.locator('#favorites-dialog').waitFor({state:'hidden'});
    await page.locator('#drafts').getByRole('button', {name:'Продолжить'}).click();
    const amount = page.locator('#favorite-form [name=amount]');
    assert.equal(await amount.inputValue(), '2000');
    assert.equal(await amount.getAttribute('max'), '100000');
    assert.ok(await amount.evaluate(el => el.checkValidity()));
    assert.ok((await page.locator('#favorite-amount-label').textContent()).includes('Съедено, г'));
    await page.locator('#favorites-dialog .close').click();
    await page.locator('#favorites-dialog').waitFor({state:'hidden'});
    await page.locator('#drafts').getByRole('button', {name:'Убрать'}).click();
    // A default-valued submission needs a draft even though the form is clean.
    await page.unroute('**/api/entries');
    await page.locator('#favorites-button').click();
    await page.locator('#favorites button').filter({hasText:'Батончик ореховый'}).click();
    const originalTime = await page.locator('#favorite-form [name=eaten_at]').inputValue();
    let committedFavorite, originalKey;
    await page.route('**/api/entries', async route => {
      if (!committedFavorite) {
        originalKey = route.request().headers()['idempotency-key'];
        committedFavorite = await (await route.fetch()).json();
      }
      await route.abort();
    });
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    await page.waitForFunction(() => document.querySelector('#favorite-form .form-error').textContent.includes('Связь прервалась'));
    assert.ok(committedFavorite.entry_id);
    await page.reload();
    await page.locator('#drafts').getByRole('button', {name:'Продолжить'}).click();
    assert.equal(await amount.inputValue(), '1');
    assert.equal(await amount.getAttribute('max'), '1000');
    assert.ok((await page.locator('#favorite-amount-label').textContent()).includes('Количество порций'));
    assert.equal(await page.locator('#favorite-form [name=eaten_at]').inputValue(), originalTime);
    // Reload opens today, whereas this draft belongs to yesterday.
    const countBeforeRetry = await page.evaluate(async day => {
      const response = await fetch(`/api/diary?day=${day}`, {headers:{Authorization:`tma ${Telegram.WebApp.initData}`}});
      if (!response.ok) throw new Error('Could not read the draft day');
      return (await response.json()).stats.entry_count;
    }, originalTime.slice(0, 10));
    await page.unroute('**/api/entries');
    const replayed = page.waitForResponse(response => response.url().endsWith('/api/entries') && response.request().method() === 'POST');
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    const replay = await replayed;
    assert.equal(replay.request().headers()['idempotency-key'], originalKey);
    assert.equal((await replay.json()).entry_id, committedFavorite.entry_id);
    await page.locator('#favorites-dialog').waitFor({state:'hidden'});
    await page.waitForFunction(() => document.getElementById('entries').getAttribute('aria-busy') === 'false');
    assert.equal(await page.locator('#entries .entry').count(), countBeforeRetry);
    assert.ok(await page.locator('#drafts').isHidden());
    // Recipes: ingredients from favorites and, on request, the product database;
    // the cooked weight sets the energy per 100 g and a portion is logged by weight.
    const shot = name => process.env.MINIAPP_SCREENSHOTS && page.screenshot({path:path.join(process.env.MINIAPP_SCREENSHOTS, name), fullPage:true});
    // A sheet is only as tall as the viewport, so grow the viewport to capture all of it.
    const sheetShot = async name => {
      if (!process.env.MINIAPP_SCREENSHOTS) return;
      const sheet = page.locator('dialog[open]');
      await sheet.evaluate(el => Promise.all(el.getAnimations().map(animation => animation.finished)));
      await page.setViewportSize({width:320, height:await sheet.evaluate(el => el.scrollHeight + 40)});
      await page.screenshot({path:path.join(process.env.MINIAPP_SCREENSHOTS, name)});
      await page.setViewportSize({width:320, height:740});
    };
    await page.locator('#recipes-button').click();
    await page.locator('#recipes .empty').waitFor();
    assert.ok(await page.evaluate(() => !Telegram.WebApp.MainButton.isVisible));
    await page.locator('#recipe-new').click();
    await page.locator('#recipe-dialog').waitFor();
    assert.ok(await page.evaluate(() => !['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)));
    assert.equal(await page.evaluate(() => Telegram.WebApp.MainButton.text), 'Сохранить блюдо');
    const ingredients = page.locator('#ingredients .ingredient');
    assert.equal(await ingredients.count(), 3);
    await page.locator('#recipe-form [name=name]').fill('Плов');
    const ingredient = (index, field) => ingredients.nth(index).locator(`[data-field=${field}]`);
    await ingredient(0, 'name').fill('бедро');
    await ingredients.nth(0).locator('.suggestion').filter({hasText:'Бедро куриное'}).waitFor();
    await sheetShot('recipe-suggestions.png');
    await ingredients.nth(0).locator('.suggestion').filter({hasText:'Бедро куриное'}).click();
    assert.equal(await ingredient(0, 'calories').inputValue(), '170');
    assert.equal(await ingredient(0, 'carbs').inputValue(), '');
    await ingredient(0, 'grams').fill('500');
    // A serving favorite of a known weight is offered per 100 g.
    await ingredient(1, 'name').fill('батончик');
    await ingredients.nth(1).locator('.suggestion').filter({hasText:'Батончик ореховый'}).click();
    assert.equal(await ingredient(1, 'calories').inputValue(), '400');
    await ingredient(1, 'grams').fill('50');
    const searchesBefore = searches.length;
    await ingredient(2, 'name').fill('гречка');
    await ingredients.nth(2).getByRole('button', {name:'Найти «гречка» в Open Food Facts'}).click();
    await ingredients.nth(2).locator('.suggestion').click();
    assert.equal(searches.length, searchesBefore + 1);
    assert.equal(await ingredient(2, 'name').inputValue(), 'Гречка (Мистраль)');
    await ingredient(2, 'grams').fill('300');
    await page.locator('#ingredient-add').click();
    assert.equal(await ingredients.count(), 4);
    assert.match(await page.locator('#recipe-raw').textContent(), /3 продукта · 850 г · 2\s103 ккал/);
    assert.equal(await page.locator('#dish-kcal').textContent(), '247');
    await page.locator('#recipe-form [name=cooked_grams]').fill('1500');
    await page.locator('#recipe-form [name=portion]').fill('300');
    assert.equal(await page.locator('#dish-kcal').textContent(), '140');
    assert.equal(await page.locator('#portion-kcal').textContent(), '421 ккал');
    assert.ok(await page.locator('#dish-note').isVisible());
    assert.equal(await page.evaluate(() => Telegram.WebApp.MainButton.text), 'Сохранить и записать 300 г');
    await sheetShot('recipe-editor.png');
    // The spare fourth row is blank and must not block saving.
    const logged = page.waitForResponse(response => response.url().endsWith('/api/recipes') && response.request().method() === 'POST');
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    const recipeResult = await (await logged).json();
    assert.equal(recipeResult.recipe.ingredients.length, 3);
    assert.equal(Math.round(recipeResult.entry.nutrition.calories), 421);
    await page.locator('#recipe-dialog').waitFor({state:'hidden'});
    await page.getByRole('button', {name:'Редактировать: Плов', exact:true}).waitFor();
    await shot('recipe-diary.png');
    // The dish is a favorite too, so the chat bot can log it by weight.
    assert.equal(await page.evaluate(async () => (await (await fetch('/api/favorites?q=плов', {headers:{Authorization:`tma ${Telegram.WebApp.initData}`}})).json()).items[0].calories_per_100g), 2103 / 15);
    await page.locator('#recipes-button').click();
    await page.locator('#recipes .recipe-card').waitFor();
    assert.match(await page.locator('#recipes .recipe-card').textContent(), /Плов140 ккал\/100 г3 ингредиента · 1,5 кг готового≈ Б 9,1 · Ж 4 · У —/);
    await sheetShot('recipe-list.png');
    await page.locator('#recipes .recipe-card').click();
    await page.locator('#recipe-dialog').waitFor();
    assert.equal(await page.locator('#recipe-title').textContent(), 'Плов');
    assert.equal(await page.evaluate(() => Telegram.WebApp.MainButton.text), 'Сохранить изменения');
    await sheetShot('recipe-existing.png');
    await ingredients.nth(1).getByRole('button', {name:'Убрать ингредиент Батончик ореховый'}).click();
    assert.equal(await ingredients.count(), 2);
    assert.equal(await page.locator('#dish-kcal').textContent(), '127');
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    await page.locator('#recipes .recipe-card').filter({hasText:'127 ккал'}).waitFor();
    // An unsaved recipe survives closing and reloading as a draft.
    await page.locator('#recipe-new').click();
    await page.locator('#recipe-form [name=name]').fill('Суп');
    await ingredient(0, 'name').fill('Вода');
    await ingredient(0, 'grams').fill('1000');
    await ingredient(0, 'calories').fill('0');
    await page.evaluate(() => Telegram.WebApp.BackButton.click());
    await page.locator('#recipe-dialog').waitFor({state:'hidden'});
    await page.reload();
    await page.locator('#drafts').getByText('Блюдо · Суп').waitFor();
    await page.locator('#drafts').getByRole('button', {name:'Продолжить'}).click();
    assert.equal(await ingredient(0, 'grams').inputValue(), '1000');
    assert.equal(await page.locator('#recipe-form [name=name]').inputValue(), 'Суп');
    await page.locator('#recipe-dialog .close').click();
    await page.locator('#recipe-dialog').waitFor({state:'hidden'});
    await page.locator('#drafts').getByRole('button', {name:'Убрать'}).click();
    await page.locator('#recipes-button').click();
    await page.locator('#recipes .recipe-card').click();
    await page.locator('#recipe-delete').click();
    await page.locator('#recipes .empty').waitFor();
    assert.equal(await page.evaluate(async () => (await (await fetch('/api/favorites?q=плов', {headers:{Authorization:`tma ${Telegram.WebApp.initData}`}})).json()).items.length), 0);
    await page.locator('#recipes-dialog .close').click();
    assert.deepEqual(errors, []);
    console.log('Mobile flow, retry, draft, edit, undo, recipes and Telegram theme: OK');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1});
