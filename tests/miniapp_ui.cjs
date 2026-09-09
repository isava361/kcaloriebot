const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || path.resolve('data/ui-check/node_modules/playwright'));

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 320, height: 740}, colorScheme: 'light'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://telegram.org/**', route => route.fulfill({contentType: 'text/javascript', body: `
      const events = {};
      const main = {setText(){return this},show(){return this},hide(){},disable(){},enable(){},showProgress(){},hideProgress(){},onClick(fn){this.click=fn}};
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
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark');
    await page.locator('#previous').click();
    await page.waitForFunction(() => !document.getElementById('next').disabled);
    const day = await page.locator('#day').inputValue();
    await page.locator('#add-button').click();
    assert.equal(await page.locator('#food-form [name=eaten_at]').inputValue(), day+'T12:00');
    // Telegram's MainButton is the only submit control while the sheet is open.
    assert.ok(await page.locator('#food-save').isHidden());
    await page.locator('#food-form [name=name]').fill('овсянка');
    await page.locator('#food-form [name=calories]').fill('370');
    await page.locator('#food-form [name=grams]').fill('60');
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
    await page.locator('#add-button').click();
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
    await page.setViewportSize({width:320,height:740});
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
