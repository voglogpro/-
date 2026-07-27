const { test, expect } = require('@playwright/test');

function state(overrides = {}) {
  const base = {
    build_version: 'browser-test',
    bot_username: 'BbGalterbot',
    support_username: 'bibitasks_support',
    can_work: false,
    is_admin: false,
    task_types: [],
    my_awards: [],
    referral: { count: 0, milestones: [] },
    referral_gate: { required: false, invited: false, confirmed: false, url: '' },
    me: {
      user_id: 101,
      name: 'Анна',
      city: 'Краснодар',
      bonus: 0,
      role: 'helper',
      status: 'new',
      applied: false,
      application_note: '',
      trust_emoji: '🌱',
      trust_name: 'Новичок',
      trust_score: 0,
      next_trust_at: 3,
      next_trust_name: 'Помощник',
      done_count: 0,
      chat_xp: 0,
      chat_xp_per_task: 50,
    },
  };
  return {
    ...base,
    ...overrides,
    me: { ...base.me, ...(overrides.me || {}) },
  };
}

async function openMiniApp(page, options = {}) {
  let currentState = options.initialState || state();
  const requests = [];
  let announcementStatusCall = 0;

  await page.route('https://telegram.org/js/telegram-web-app.js', route =>
    route.fulfill({ status: 200, contentType: 'application/javascript', body: '' })
  );
  await page.addInitScript(startParam => {
    window.Telegram = {
      WebApp: {
        initData: 'query_id=browser-test',
        initDataUnsafe: {
          user: { id: 101, first_name: 'Анна', allows_write_to_pm: true },
          start_param: startParam,
        },
        colorScheme: 'light',
        ready() {},
        expand() {},
        setHeaderColor() {},
        setBackgroundColor() {},
        HapticFeedback: {
          impactOccurred() {},
          notificationOccurred() {},
        },
        openTelegramLink(url) { window.__openedTelegramUrl = url; },
        requestWriteAccess(callback) { if (callback) callback(true); },
      },
    };
  }, options.startParam || '');
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    let body = null;
    try { body = request.postDataJSON(); } catch (_) {}
    requests.push({ method: request.method(), path: url.pathname, query: url.search, body });

    if (url.pathname === '/api/state') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(currentState) });
    }
    if (url.pathname === '/api/apply') {
      currentState = state({
        ...currentState,
        me: { ...currentState.me, applied: true, status: 'pending' },
      });
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' });
    }
    if (url.pathname === '/api/tasks/available' && options.tasksGate) {
      await options.tasksGate;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ mine: [], available: [] }),
      });
    }
    if (url.pathname === '/api/wallet' && options.walletFailure) {
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ message: 'Кошелёк временно недоступен' }),
      });
    }
    if (url.pathname === '/api/admin/task/announcement/status' && options.announcementStatuses) {
      const sequence = options.announcementStatuses;
      const value = typeof sequence === 'function'
        ? sequence(announcementStatusCall)
        : sequence[Math.min(announcementStatusCall, sequence.length - 1)];
      announcementStatusCall += 1;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(value),
      });
    }
    if (url.pathname === '/api/admin/overview' && options.adminOverview) {
      const overview = typeof options.adminOverview === 'function'
        ? options.adminOverview()
        : options.adminOverview;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(overview),
      });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
  });

  await page.goto('/index.html');
  await expect(page.locator('#app')).toBeVisible();
  return { requests, setState(value) { currentState = value; } };
}

test('новичок отправляет заявку без номера телефона', async ({ page }) => {
  const harness = await openMiniApp(page);

  await expect(page.locator('#applyBox')).toBeVisible();
  await page.locator('#apName').fill('');
  await page.locator('#apCity').fill('');
  await page.locator('#apSend').click();
  await expect(page.locator('#apName')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.locator('#apName')).toBeFocused();
  await expect(page.locator('#apError')).toContainText('Укажи имя');
  await page.locator('#apName').fill('Анна');
  await page.locator('#apCity').fill('Краснодар');
  await page.locator('#apAbout').fill('Могу поправлять парковки и фотографировать результат');
  await page.locator('#apSend').click();

  await expect(page.locator('#waitBox')).toBeVisible();
  const application = harness.requests.find(item => item.path === '/api/apply');
  expect(application.body).toEqual({
    name: 'Анна',
    city: 'Краснодар',
    about: 'Могу поправлять парковки и фотографировать результат',
  });
  expect(JSON.stringify(application.body)).not.toContain('phone');
});

test('светлая и тёмная темы меняют фон и цвет текста', async ({ page }) => {
  await openMiniApp(page);

  await page.locator('#themeToggle').click();
  await page.locator('[data-theme-pick="dark"]').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.locator('body')).toHaveCSS('color', 'rgb(255, 255, 255)');
  await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(9, 12, 10)');

  await page.locator('[data-theme-pick="light"]').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(page.locator('body')).toHaveCSS('color', 'rgb(15, 21, 18)');
  await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
});

test('Escape отменяет обязательный ввод, а пустое значение не принимается', async ({ page }) => {
  await openMiniApp(page);
  await page.evaluate(() => {
    window.__askResult = 'pending';
    askText({ title: 'ID получателя', required: true, okLabel: 'Продолжить' })
      .then(value => { window.__askResult = value; });
  });

  await expect(page.locator('#askSheet')).toBeVisible();
  await page.locator('#askOk').click();
  await expect(page.locator('#askSheet')).toBeVisible();
  await expect(page.locator('#askText')).toBeFocused();
  await expect(page.locator('#askText')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.locator('#askError')).not.toBeEmpty();
  await page.keyboard.press('Escape');

  await expect(page.locator('#askSheet')).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.__askResult)).toBe(null);
});

test('вложенная административная шторка возвращает фокус на видимую вкладку', async ({ page }) => {
  await openMiniApp(page, { initialState: state({ is_admin: true }) });
  const adminTab = page.locator('#nav [data-tab="tab-admin"]');
  await adminTab.click();

  await page.evaluate(() => document.querySelector('#memberSheet').classList.remove('hidden'));
  await expect(page.locator('#msPlus')).toBeFocused();
  await page.locator('#msPlus').focus();
  await page.evaluate(() => {
    document.querySelector('#memberSheet').classList.add('hidden');
    document.querySelector('#balanceSheet').classList.remove('hidden');
  });
  await page.locator('#balanceCancel').click();

  await expect(adminTab).toBeFocused();
  await expect(page.locator('#memberSheet')).toBeHidden();
  await expect(page.locator('#balanceSheet')).toBeHidden();
});

test('все поля Mini App имеют программное доступное название', async ({ page }) => {
  await openMiniApp(page, { initialState: state({ is_admin: true }) });
  const missing = await page.locator('input, select, textarea').evaluateAll(controls =>
    controls.filter(control => {
      if (control.type === 'hidden') return false;
      if (control.getAttribute('aria-label')) return false;
      const labelledBy = (control.getAttribute('aria-labelledby') || '')
        .split(/\s+/).filter(Boolean)
        .some(id => document.getElementById(id)?.textContent.trim());
      if (labelledBy) return false;
      return !control.id || !document.querySelector(`label[for="${CSS.escape(control.id)}"]`);
    }).map(control => control.id || control.outerHTML.slice(0, 80))
  );
  expect(missing).toEqual([]);
});

test('мастер задания связывает ошибку с полем и переводит фокус', async ({ page }) => {
  await openMiniApp(page, { initialState: state({ is_admin: true }) });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('#ntTitle').fill('');
  await page.locator('#ntCreate').click();

  await expect(page.locator('#ntTitle')).toBeFocused();
  await expect(page.locator('#ntTitle')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.locator('#ntTitle')).toHaveAttribute('aria-describedby', /ntError/);
  await expect(page.locator('#ntError')).not.toBeEmpty();
});

test('фотоотчёт отмечает обязательное фото и комментарий доступной ошибкой', async ({ page }) => {
  await openMiniApp(page);

  await page.evaluate(() => openFinishReport({ id: 17, assignment_id: 3, evidence_policy: 'after_required' }, null));
  await page.locator('#finishSubmit').click();
  await expect(page.locator('#finishPhotos')).toBeFocused();
  await expect(page.locator('#finishPhotos')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.locator('#finishPhotoError')).not.toBeEmpty();

  await page.evaluate(() => openFinishReport({ id: 18, assignment_id: 4, evidence_policy: 'comment_only' }, null));
  await page.locator('#finishSubmit').click();
  await expect(page.locator('#finishNote')).toBeFocused();
  await expect(page.locator('#finishNote')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.locator('#finishNote')).toHaveAttribute('aria-describedby', /finishPhotoError/);
});

test('разделы скаута отражают выбранное состояние без неполного tab-паттерна', async ({ page }) => {
  await openMiniApp(page, { initialState: state({ is_admin: true }) });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  const create = page.locator('[data-asub="adSubCreate"]');
  await create.click();

  await expect(create).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('[data-asub="adSubQueue"]')).toHaveAttribute('aria-pressed', 'false');
  await expect(create).not.toHaveAttribute('role', 'tab');
});

test('загрузка заданий меняет aria-busy с true на false', async ({ page }) => {
  let releaseTasks;
  const tasksGate = new Promise(resolve => { releaseTasks = resolve; });
  await openMiniApp(page, {
    initialState: state({ can_work: true, me: { status: 'approved' } }),
    tasksGate,
  });

  await expect(page.locator('#tab-tasks')).toHaveAttribute('aria-busy', 'true');
  releaseTasks();
  await expect(page.locator('#tab-tasks')).toHaveAttribute('aria-busy', 'false');
});

test('ошибка кошелька оставляет перевод выключенным и показывает один alert с retry', async ({ page }) => {
  await openMiniApp(page, { walletFailure: true });
  await page.locator('#nav [data-tab="tab-wallet"]').click();

  await expect(page.locator('#wWithdraw')).toBeDisabled();
  await expect(page.locator('#wError')).toContainText('Кошелёк временно недоступен');
  await expect(page.locator('#wHistory .retry')).toBeVisible();
  await expect(page.locator('#wHistory [role="alert"]')).toHaveCount(0);
});

test('скаут открывает доставленное OPS-сообщение и видит честный lease кассира', async ({ page }) => {
  await openMiniApp(page, {
    initialState: state({ is_admin: true }),
    adminOverview: {
      pending: [], pending_total: 0, rejected: [], review: [], review_total: 0,
      team: [], awards: [], granted: [], task_templates: [],
      open_tasks: [{
        id: 44, title: 'Поправить парковку', type_title: 'Парковка', emoji: '🚲',
        reward: 120, status: 'open', city: 'Краснодар', address: 'ТЦ Центр',
        announcement_status: 'sent',
        announcement_url: 'https://t.me/c/9000002222/17/845',
      }],
      withdrawals: [{
        id: 71, user_id: 501, full_name: 'Получатель один', amount: 1000,
        status: 'processing', created_at: '2026-07-28T00:00:00+00:00',
        processing_by: 777, processing_name: 'Анна Кассир',
        lease_state: 'held_by_other', lease_remaining_seconds: 120,
        can_continue: false, can_release: false, can_takeover: false,
      }, {
        id: 72, user_id: 502, full_name: 'Получатель два', amount: 1000,
        status: 'processing', created_at: '2026-07-28T00:00:00+00:00',
        processing_by: 778, processing_name: 'Кассир смены',
        lease_state: 'expired', lease_remaining_seconds: 0,
        can_continue: false, can_release: false, can_takeover: true,
      }],
    },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();

  const openMessage = page.locator('[data-topen]');
  await expect(openMessage).toBeVisible();
  await openMessage.click();
  await expect.poll(() => page.evaluate(() => window.__openedTelegramUrl)).toBe(
    'https://t.me/c/9000002222/17/845'
  );
  await page.locator('[data-asub="adSubAwards"]').click();

  const held = page.locator('#adWithdrawals .task').filter({ hasText: 'Получатель один' });
  await expect(held).toContainText('Кассир: Анна Кассир');
  await expect(held).toContainText('можно забрать через');
  const waitingTakeover = held.getByRole('button', { name: 'Забрать после тайм-аута' });
  await expect(waitingTakeover).toBeDisabled();
  await expect(waitingTakeover).toHaveAttribute('aria-describedby', 'lease-71');

  const expired = page.locator('#adWithdrawals .task').filter({ hasText: 'Получатель два' });
  await expect(expired).toContainText('Срок Кассир смены истёк');
  await expect(expired.getByRole('button', { name: 'Забрать заявку' })).toBeEnabled();
});

test('статус публикации OPS обновляется лёгким запросом и даёт ссылку', async ({ page }) => {
  let overviewCalls = 0;
  const overview = () => {
    overviewCalls += 1;
    const delivered = overviewCalls > 1;
    return {
      pending: [], pending_total: 0, rejected: [], review: [], review_total: 0,
      team: [], awards: [], granted: [], withdrawals: [], task_templates: [],
      open_tasks: [{
        id: 45, title: 'Поправить парковку', type_title: 'Парковка', emoji: '🚲',
        reward: 120, status: 'open', city: 'Краснодар', address: 'ТЦ Центр',
        announcement_status: delivered ? 'sent' : 'pending',
        announcement_url: delivered ? 'https://t.me/c/9000002222/17/846' : '',
      }],
    };
  };
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }),
    adminOverview: overview,
    announcementStatuses: [{
      ok: true,
      items: [{
        task_id: 45, status: 'sent', attempts: 1, error: '',
        sent_at: '2026-07-28T12:00:00+00:00', message_id: 846, thread_id: 17,
        url: 'https://t.me/c/9000002222/17/846',
      }],
    }],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await expect(page.locator('[data-topen]')).toHaveCount(0);
  await expect.poll(
    () => harness.requests.filter(item => item.path === '/api/admin/task/announcement/status').length,
    { timeout: 7000 }
  ).toBe(1);
  await expect(page.locator('[data-topen]')).toBeVisible();
  expect(overviewCalls).toBe(2);
});

test('таймер выплаты проходит через ноль и включает безопасный takeover', async ({ page }) => {
  let overviewCalls = 0;
  const overview = () => {
    overviewCalls += 1;
    const expired = overviewCalls > 1;
    return {
      pending: [], pending_total: 0, rejected: [], review: [], review_total: 0,
      team: [], awards: [], granted: [], open_tasks: [], task_templates: [],
      withdrawals: [{
        id: 73, user_id: 503, full_name: 'Получатель три', amount: 1000,
        status: 'processing', created_at: '2026-07-28T00:00:00+00:00',
        processing_by: 779, processing_name: 'Кассир ночной смены',
        lease_state: expired ? 'expired' : 'held_by_other',
        lease_remaining_seconds: expired ? 0 : 30,
        can_continue: false, can_release: false, can_takeover: expired,
      }],
    };
  };
  await openMiniApp(page, {
    initialState: state({ is_admin: true }),
    adminOverview: overview,
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  const withdrawals = page.locator('#adWithdrawals');
  await expect(withdrawals.getByRole('button', { name: 'Забрать после тайм-аута' })).toBeDisabled();
  await expect(withdrawals.locator('[data-lease-seconds]')).toHaveAttribute('aria-hidden', 'true');
  await expect(withdrawals.locator('.sr-only')).toContainText('меньше минуты');
  await page.evaluate(() => {
    const timer = document.querySelector('[data-lease-seconds]');
    timer.dataset.leaseDeadline = String(performance.now() + 50);
  });
  await expect.poll(() => overviewCalls, { timeout: 4000 }).toBeGreaterThanOrEqual(2);
  await expect(withdrawals.getByRole('button', { name: 'Забрать заявку' })).toBeEnabled();
});
