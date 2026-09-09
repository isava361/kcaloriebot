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
    assert.equal(await page.locator('#entries .meal-head').count(), 1);
    assert.equal(await page.locator('#week .week-day').count(), 7);
    assert.equal(await page.locator('#day').inputValue(), day);
    await page.getByRole('button', {name:'Редактировать: овсянка',exact:true}).click();
    await page.locator('#food-form [name=grams]').fill('100');
    await page.evaluate(() => Telegram.WebApp.MainButton.click());
    await page.waitForFunction(() => document.getElementById('calories').textContent === '370');
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
    assert.deepEqual(errors, []);
    console.log('Mobile flow, retry, draft, edit, undo and Telegram theme: OK');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1});
