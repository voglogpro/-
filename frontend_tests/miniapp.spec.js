const { test, expect } = require('@playwright/test');

function state(overrides = {}) {
  const base = {
    build_version: 'browser-test',
    bot_username: 'BbGalterbot',
    support_username: 'bibitasks_support',
    privacy_url: '',
    help: {
      community_url: 'https://t.me/bbbikefan',
      work_topic_url: 'https://t.me/bbbikefan/4',
      bot_url: 'https://t.me/BbGalterbot',
      support_url: 'https://t.me/bibitasks_support',
    },
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
  let approveCall = 0;
  let awardGrantCall = 0;
  let manualGrantCall = 0;
  let manualReversalCall = 0;
  let applyCall = 0;
  let memberSearchCall = 0;
  let taskCreateCall = 0;
  let stateCall = 0;
  let lastAdminOverview = null;

  await page.route('https://telegram.org/js/telegram-web-app.js', route =>
    route.fulfill({ status: 200, contentType: 'application/javascript', body: '' })
  );
  await page.addInitScript(config => {
    window.Telegram = {
      WebApp: {
        initData: 'query_id=browser-test',
        initDataUnsafe: {
          user: {
            id: 101, first_name: 'Анна',
            allows_write_to_pm: config.writeAccessResult === false ? false : true,
          },
          start_param: config.startParam,
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
        requestWriteAccess(callback) { if (callback) callback(config.writeAccessResult !== false); },
      },
    };
  }, {
    startParam: options.startParam || '',
    writeAccessResult: options.writeAccessResult,
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    let body = null;
    try { body = request.postDataJSON(); } catch (_) {}
    requests.push({ method: request.method(), path: url.pathname, query: url.search, body });

    if (url.pathname === '/api/state') {
      if (options.stateResponses) {
        const response = options.stateResponses[
          Math.min(stateCall, options.stateResponses.length - 1)
        ];
        stateCall += 1;
        if (response.abort) return route.abort(response.abort);
        return route.fulfill({
          status: response.status,
          contentType: 'application/json',
          body: JSON.stringify(response.body || {}),
        });
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(currentState) });
    }
    if (url.pathname === '/api/apply') {
      if (options.applyResponses) {
        const value = options.applyResponses[
          Math.min(applyCall, options.applyResponses.length - 1)
        ];
        applyCall += 1;
        if (value.commit) {
          currentState = state({
            ...currentState,
            me: { ...currentState.me, applied: true, status: 'pending' },
          });
        }
        if (value.abort) return route.abort(value.abort);
        return route.fulfill({
          status: value.status,
          contentType: 'application/json',
          body: JSON.stringify(value.body || {}),
        });
      }
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
    if (url.pathname === '/api/tasks/available' && options.tasksData) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(options.tasksData),
      });
    }
    if (url.pathname === '/api/tasks/context' && options.taskContext) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(options.taskContext),
      });
    }
    if (url.pathname === '/api/profile/city') {
      currentState = state({
        ...currentState,
        me: {
          ...currentState.me,
          city_change_requested: body.action === 'cancel' ? '' : body.city,
          city_change_requested_at: body.action === 'cancel' ? '' : '2026-07-28T12:00:00+00:00',
        },
      });
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true, city: currentState.me.city,
          requested_city: body.city, requested_at: currentState.me.city_change_requested_at,
          pending: body.action !== 'cancel',
        }),
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
      lastAdminOverview = overview;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(overview),
      });
    }
    if (url.pathname === '/api/admin/members' && options.adminOverview) {
      const overview = lastAdminOverview || (
        typeof options.adminOverview === 'function' ? options.adminOverview() : options.adminOverview
      );
      if (options.memberSearchResponses) {
        const value = options.memberSearchResponses[
          Math.min(memberSearchCall, options.memberSearchResponses.length - 1)
        ];
        memberSearchCall += 1;
        if (value.delay) await new Promise(resolve => setTimeout(resolve, value.delay));
        return route.fulfill({
          status: value.status || 200,
          contentType: 'application/json',
          body: JSON.stringify(value.body || {}),
        });
      }
      const team = overview.team || [];
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: team, team, total: team.length, next_cursor: null }),
      });
    }
    if (url.pathname === '/api/awards' && options.adminOverview) {
      const overview = lastAdminOverview || (
        typeof options.adminOverview === 'function' ? options.adminOverview() : options.adminOverview
      );
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ catalog: overview.awards || [], mine: [] }),
      });
    }
    if (url.pathname === '/api/admin/task/approve' && options.approveResponses) {
      const sequence = options.approveResponses;
      const value = sequence[Math.min(approveCall, sequence.length - 1)];
      approveCall += 1;
      return route.fulfill({
        status: value.status,
        contentType: 'application/json',
        body: JSON.stringify(value.body || {}),
      });
    }
    if (url.pathname === '/api/admin/task/create' && options.taskCreateResponses) {
      const sequence = options.taskCreateResponses;
      const value = sequence[Math.min(taskCreateCall, sequence.length - 1)];
      taskCreateCall += 1;
      return route.fulfill({
        status: value.status,
        contentType: 'application/json',
        body: JSON.stringify(value.body || {}),
      });
    }
    if (url.pathname === '/api/admin/award/grant' && options.awardGrantResponses) {
      const sequence = options.awardGrantResponses;
      const value = sequence[Math.min(awardGrantCall, sequence.length - 1)];
      awardGrantCall += 1;
      return route.fulfill({
        status: value.status,
        contentType: 'application/json',
        body: JSON.stringify(value.body || {}),
      });
    }
    if (url.pathname === '/api/admin/grant' && options.manualGrantResponses) {
      const sequence = options.manualGrantResponses;
      const value = sequence[Math.min(manualGrantCall, sequence.length - 1)];
      manualGrantCall += 1;
      return route.fulfill({
        status: value.status,
        contentType: 'application/json',
        body: JSON.stringify(value.body || {}),
      });
    }
    if (url.pathname === '/api/admin/grant/reversal' && options.manualReversalResponses) {
      const sequence = options.manualReversalResponses;
      const value = sequence[Math.min(manualReversalCall, sequence.length - 1)];
      manualReversalCall += 1;
      return route.fulfill({
        status: value.status,
        contentType: 'application/json',
        body: JSON.stringify(value.body || {}),
      });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
  });

  await page.goto('/index.html');
  if (!options.expectInitialFailure) await expect(page.locator('#app')).toBeVisible();
  return { requests, setState(value) { currentState = value; } };
}

test('первый вход отличает ошибку подписи Telegram от аварии сервиса', async ({ page }) => {
  await openMiniApp(page, {
    expectInitialFailure: true,
    stateResponses: [{ status: 401, body: { message: 'Нет подписи Telegram' } }],
  });
  await expect(page.locator('#loading')).toContainText('Открой приложение из Telegram');
  await page.locator('#loadBot').click();
  expect(await page.evaluate(() => window.__openedTelegramUrl)).toBe(
    'https://t.me/BbGalterbot?start=help'
  );
});

test('ошибка backend не выдаётся за неверный вход и retry восстанавливает экран', async ({ page }) => {
  await openMiniApp(page, {
    expectInitialFailure: true,
    stateResponses: [
      { status: 503, body: { message: 'Временная ошибка' } },
      { status: 200, body: state() },
    ],
  });
  await expect(page.locator('#loading')).toContainText('Сервис временно недоступен');
  await expect(page.locator('#loading')).not.toContainText('Открой приложение из Telegram');
  await page.locator('#loadRetry').click();
  await expect(page.locator('#app')).toBeVisible();
});

test('сетевая ошибка показывает проверку связи и даёт повторить', async ({ page }) => {
  await openMiniApp(page, {
    expectInitialFailure: true,
    stateResponses: [
      { abort: 'internetdisconnected' },
      { status: 200, body: state() },
    ],
  });
  await expect(page.locator('#loading')).toContainText('Нет связи с сервисом');
  await page.locator('#loadRetry').click();
  await expect(page.locator('#app')).toBeVisible();
});

test('ошибка повторной загрузки скрывает устаревший экран и retry восстанавливает приложение', async ({ page }) => {
  await openMiniApp(page, {
    stateResponses: [
      { status: 200, body: state() },
      { status: 503, body: { message: 'Временная ошибка' } },
      { status: 200, body: state() },
    ],
  });
  await page.evaluate(() => load());
  await expect(page.locator('#loading')).toBeVisible();
  await expect(page.locator('#loading')).toContainText('Сервис временно недоступен');
  await expect(page.locator('#app')).toBeHidden();
  await expect(page.locator('#nav')).toBeHidden();
  await page.locator('#loadRetry').click();
  await expect(page.locator('#app')).toBeVisible();
  await expect(page.locator('#loading')).toBeHidden();
});

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
  await expect(page.locator('#participantRegister')).toContainText('1000 бонусов ≈ 118 минут');
  await expect(page.locator('#privacyNotice')).toContainText('заявки и подбора заданий');
  await expect(page.locator('#privacyNotice')).toContainText('Финансовая история сохраняется');
  await expect(page.locator('#privacyNotice a')).toHaveCount(0);
  await expect(page.locator('#waitMessage')).not.toContainText('до одного дня');
});

test('неоднозначная отправка анкеты восстанавливается через application_pending', async ({ page }) => {
  const harness = await openMiniApp(page, {
    applyResponses: [
      { abort: 'internetdisconnected', commit: true },
      { status: 409, body: { error: 'application_pending', message: 'Заявка уже на рассмотрении.' } },
    ],
  });
  await page.locator('#apName').fill('Анна');
  await page.locator('#apCity').fill('Краснодар');
  await page.locator('#apAbout').fill('Могу поправлять парковки и делать фото');
  await page.locator('#apSend').click();
  await expect(page.locator('#apSend')).toBeEnabled();
  await page.locator('#apSend').click();
  await expect(page.locator('#waitBox')).toBeVisible();
  expect(harness.requests.filter(item => item.path === '/api/apply')).toHaveLength(2);
});

test('ожидающий участник может вручную обновить статус без polling', async ({ page }) => {
  const harness = await openMiniApp(page, {
    initialState: state({ me: { applied: true, status: 'pending', role: 'applicant' } }),
  });
  await expect(page.locator('#waitRefresh')).toBeVisible();
  harness.setState(state({ can_work: true, me: { applied: true, status: 'approved' } }));
  await page.locator('#waitRefresh').click();
  await expect(page.locator('#worksBox')).toBeVisible();
  await expect(page.locator('#waitBox')).toBeHidden();
});

test('поля анкеты показывают и соблюдают пределы', async ({ page }) => {
  await openMiniApp(page);
  await expect(page.locator('#apName')).toHaveAttribute('maxlength', '80');
  await expect(page.locator('#apCity')).toHaveAttribute('maxlength', '80');
  await expect(page.locator('#apAbout')).toHaveAttribute('maxlength', '600');
  await expect(page.locator('#finishNote')).toHaveAttribute('maxlength', '300');
  await page.locator('#apAbout').fill('а'.repeat(600));
  await expect(page.locator('#apAboutCount')).toHaveText('600 из 600');
});

test('отклонённую анкету можно явно исправить только после суточного лимита', async ({ page }) => {
  const rejected = state({
    me: {
      status: 'blocked', role: 'applicant', applied: true,
      application_note: 'Опиши конкретнее', about: 'Могу помогать',
      can_resubmit: true, resubmit_retry_after: 0,
    },
  });
  const harness = await openMiniApp(page, { initialState: rejected });

  await expect(page.locator('#blockedBox')).toContainText('Опиши конкретнее');
  await page.locator('#blockedReapply').click();
  await expect(page.locator('#applyBox')).toBeVisible();
  await expect(page.locator('#apName')).toHaveValue('Анна');
  await expect(page.locator('#apCity')).toHaveValue('Краснодар');
  await expect(page.locator('#apAbout')).toHaveValue('Могу помогать');
  await expect(page.locator('#apSend')).toHaveText('Отправить повторную заявку');
  await page.locator('#apAbout').fill('Могу поправлять парковки и делать фотоотчёт');
  await page.locator('#apSend').click();
  await expect(page.locator('#waitBox')).toBeVisible();
  expect(harness.requests.filter(item => item.path === '/api/apply')).toHaveLength(1);

  const cooldown = state({
    me: {
      status: 'blocked', role: 'applicant', applied: true,
      can_resubmit: false, resubmit_retry_after: 7200,
    },
  });
  harness.setState(cooldown);
  await page.reload();
  await expect(page.locator('#app')).toBeVisible();
  await expect(page.locator('#blockedReapply')).toBeHidden();
  await expect(page.locator('#blockedCooldown')).toContainText('через 2 ч');
});

test('помощь одобренного участника содержит безопасные рабочие ссылки', async ({ page }) => {
  await openMiniApp(page, {
    initialState: state({
      can_work: true,
      privacy_url: 'https://example.org/privacy',
      me: { status: 'approved' },
    }),
    tasksData: { available: [], mine: [] },
  });

  await page.locator('#nav [data-tab="tab-profile"]').click();
  await expect(page.locator('#pHelpCard')).toContainText('Как выполнять задания');
  await expect(page.locator('#pHelpLinks')).toContainText('Рабочая тема');
  await expect(page.locator('#pHelpLinks')).toContainText('Ответственный');
  await expect(page.locator('#privacyNotice a')).toHaveAttribute('href', 'https://example.org/privacy');
  await expect(page.locator('#privacyNotice a')).toHaveText('Сроки и порядок удаления');
  await page.getByRole('button', { name: '🛠 Рабочая тема' }).click();
  expect(await page.evaluate(() => window.__openedTelegramUrl)).toBe('https://t.me/bbbikefan/4');
});

test('пустой список заданий ведёт к инструкции и рабочей теме', async ({ page }) => {
  await openMiniApp(page, {
    initialState: state({ can_work: true, me: { status: 'approved' } }),
    tasksData: { available: [], mine: [] },
  });
  await expect(page.locator('#availList')).toContainText('Свободных заданий сейчас нет');
  await expect(page.locator('#emptyTasksWork')).toBeVisible();
  await page.locator('#emptyTasksHelp').click();
  await expect(page.locator('#tab-profile')).toBeVisible();
  await expect(page.locator('#pHelpCard')).toContainText('Проверь город, адрес, срок');
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

  const contrast = await page.evaluate(() => {
    const button = document.querySelector('.seg button.on');
    const parse = value => value.match(/\d+/g).slice(0, 3).map(Number);
    const luminance = value => {
      const channels = parse(value).map(item => item / 255).map(item =>
        item <= .04045 ? item / 12.92 : ((item + .055) / 1.055) ** 2.4
      );
      return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
    };
    const style = getComputedStyle(button);
    const foreground = luminance(style.color), background = luminance(style.backgroundColor);
    return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
  });
  expect(contrast).toBeGreaterThanOrEqual(4.5);
});

test('исполнитель до взятия видит формат отчёта, а после возврата — причину', async ({ page }) => {
  await openMiniApp(page, {
    initialState: state({ can_work: true, me: { status: 'approved' } }),
    tasksData: {
      available: [{
        id: 17, title: 'Поправить парковку', type_title: 'Парковка',
        city: 'Краснодар', address: 'ТЦ Центр', reward: 80,
        evidence_policy: 'after_required', status: 'open',
      }, {
        id: 19, title: 'Проверить адрес', type_title: 'Осмотр',
        city: 'Краснодар', address: 'ул. Мира', reward: 30,
        evidence_policy: 'comment_only', status: 'open',
      }],
      mine: [{
        id: 18, assignment_id: 9, title: 'Проверить байки', type_title: 'Осмотр',
        city: 'Краснодар', address: 'ул. Красная', reward: 60,
        evidence_policy: 'comment_only', status: 'claimed',
        review_note: 'Добавь точный номер парковки',
      }],
    },
  });

  await expect(page.locator('[data-card="17"]')).toContainText('Отчёт: 1–4 фото');
  await expect(page.locator('[data-card="18"]')).toContainText('Отчёт: комментарий');
  await expect(page.locator('[data-card="18"]')).toContainText('Вернули на доработку');
  await expect(page.locator('[data-card="18"]')).toContainText('Добавь точный номер парковки');
  await page.locator('[data-claim="19"]').click();
  await expect(page.locator('#toast')).toContainText('добавь комментарий');
});

test('не найденное задание по прямой ссылке объясняет причину', async ({ page }) => {
  await openMiniApp(page, {
    startParam: 'task_404',
    initialState: state({ can_work: true, me: { status: 'approved' } }),
    tasksData: { available: [], mine: [] },
    taskContext: {
      ok: true, reason: 'city_mismatch',
      message: 'Задание относится к другому городу. Проверь город в профиле.',
    },
  });

  await expect(page.locator('#availList [role="status"]')).toContainText('другому городу');
});

test('одобренный участник запрашивает смену города без обхода проверки', async ({ page }) => {
  const harness = await openMiniApp(page, {
    initialState: state({ can_work: true, me: { status: 'approved', city: 'Краснодар' } }),
    tasksData: { available: [], mine: [] },
  });
  await page.locator('#nav [data-tab="tab-profile"]').click();
  await page.locator('#pCity').fill('Орёл');
  await page.locator('#pCitySave').click();

  await expect.poll(() => harness.requests.some(item =>
    item.path === '/api/profile/city' && item.body.city === 'Орёл'
  )).toBe(true);
  await expect(page.locator('#pCity')).toHaveValue('Краснодар');
  await expect(page.locator('#pCityPending')).toContainText('Ожидает подтверждения: Орёл');
  await page.locator('#pCityCancel').click();
  await expect(page.locator('#pCityPending')).toBeHidden();
  expect(harness.requests.some(item =>
    item.path === '/api/profile/city' && item.body.action === 'cancel'
    && item.body.requested_at === '2026-07-28T12:00:00+00:00'
  )).toBe(true);
});

test('отказ Telegram в уведомлениях не скрывает способ вернуться к боту', async ({ page }) => {
  await openMiniApp(page, { writeAccessResult: false });
  await page.locator('#apName').fill('Анна');
  await page.locator('#apCity').fill('Краснодар');
  await page.locator('#apAbout').fill('Могу поправлять парковки');
  await page.locator('#apSend').click();

  await expect(page.locator('#waitMessage')).toContainText('Telegram не разрешил уведомления');
  await expect(page.locator('#waitBot')).toBeVisible();
});

test('скаут видит честную проверку без фото и открывает доказательство полностью', async ({ page }) => {
  const overview = {
    pending: [], pending_total: 0, rejected: [], review_total: 2,
    team: [], awards: [], granted: [], withdrawals: [], open_tasks: [],
    task_templates: [], recent_decisions: [],
    review: [{
      id: 31, assignment_id: 301, title: 'Комментарий', type_title: 'Проверка',
      city: 'Краснодар', address: 'Центр', reward: 40, status: 'review',
      evidence_policy: 'comment_only', proof_note: 'Всё проверено',
      can_approve: false, approval_block_reason: 'Задание создал этот ответственный',
    }, {
      id: 32, assignment_id: 302, title: 'Фото парковки', type_title: 'Парковка',
      city: 'Краснодар', address: 'Вокзал', reward: 80, status: 'review',
      evidence_policy: 'after_required', can_approve: true,
      proof_photos: ['data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='],
    }],
  };
  await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();

  await expect(page.locator('#adReview')).toContainText('Фото не требовалось');
  await expect(page.getByRole('button', { name: 'Нужен второй ответственный' })).toBeDisabled();
  await page.locator('#adReview [data-evidence-url]').click();
  await expect(page.locator('#evidenceSheet')).toBeVisible();
  await expect(page.locator('#evidenceFull')).toHaveAttribute('src', /^data:image\/gif/);
  await page.keyboard.press('Escape');
  await expect(page.locator('#evidenceSheet')).toBeHidden();
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

  await page.locator('#ntTitle').fill('Поправить парковку');
  await page.locator('#ntCreate').click();
  await expect(page.locator('.wizard-step[data-wstep="2"] h2')).toBeFocused();
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

test('поздний ответ старого поиска не перезаписывает новый результат', async ({ page }) => {
  const ivan = { user_id: 501, name: 'Иван', role: 'helper', city: 'Краснодар', bonus: 10, done_count: 1, chat_xp: 0, tags: [], trust_emoji: '🌱' };
  const olga = { user_id: 502, name: 'Ольга', role: 'helper', city: 'Краснодар', bonus: 20, done_count: 2, chat_xp: 0, tags: [], trust_emoji: '🌱' };
  await openMiniApp(page, {
    initialState: state({ is_admin: true }),
    adminOverview: {
      pending: [], pending_total: 0, rejected: [], city_changes: [], review: [], review_total: 0,
      recent_decisions: [], open_tasks: [], awards: [], granted: [], withdrawals: [],
      task_templates: [], role_changes: [], manual_grants: [], team: [ivan],
    },
    memberSearchResponses: [
      { body: { items: [ivan], total: 1 } },
      { delay: 600, body: { items: [ivan], total: 1 } },
      { delay: 10, body: { items: [olga], total: 1 } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubTeam"]').click();
  await expect(page.locator('[data-member="501"]')).toBeVisible();
  await page.locator('#adTeamSearch').fill('Иван');
  await page.waitForTimeout(330);
  await page.locator('#adTeamSearch').fill('Ольга');
  await expect(page.locator('[data-member="502"]')).toBeVisible();
  await page.waitForTimeout(650);
  await expect(page.locator('[data-member="502"]')).toBeVisible();
  await expect(page.locator('[data-member="501"]')).toHaveCount(0);
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
      }, {
        id: 74, user_id: 504, full_name: 'Получатель смены', amount: 1000,
        status: 'processing', created_at: '2026-07-28T00:00:00+00:00',
        processing_by: null, processing_name: '',
        lease_state: 'unassigned', lease_remaining_seconds: 0,
        can_continue: true, can_release: false, can_takeover: false,
        can_reject: false,
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

  const unassigned = page.locator('#adWithdrawals .task').filter({ hasText: 'Получатель смены' });
  await expect(unassigned.getByRole('button', { name: 'Продолжить перевод' })).toBeEnabled();
  await expect(unassigned.getByRole('button', { name: 'Отклонить' })).toHaveCount(0);
});

test('мастер не разрешает опубликовать задание с прошедшим окончанием', async ({ page }) => {
  await openMiniApp(page, { initialState: state({ is_admin: true }) });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.evaluate(() => {
    const local = ms => {
      const value = new Date(Date.now() + ms);
      return new Date(value.getTime() - value.getTimezoneOffset() * 60000)
        .toISOString().slice(0, 16);
    };
    document.getElementById('ntTitle').value = 'Поправить парковку';
    document.getElementById('ntCity').value = 'Краснодар';
    document.getElementById('ntAddr').value = 'ул. Красная, 1';
    document.getElementById('ntSlotStart').value = local(-7200000);
    document.getElementById('ntSlotEnd').value = local(-3600000);
    document.getElementById('ntReward').value = '80';
    document.getElementById('ntBudgetCap').value = '80';
    setWizardStep(3);
  });
  await page.locator('#ntCreate').click();

  await expect(page.locator('.wizard-step[data-wstep="2"]')).toBeVisible();
  await expect(page.locator('#ntSlotEnd')).toBeFocused();
  await expect(page.locator('#ntSlotEnd')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.locator('#ntError')).toHaveText('Конец задания должен быть в будущем.');
  await expect(page.locator('#ntSlotEnd')).toHaveAttribute('min', /T/);
});

test('создание задания повторяет неоднозначный запрос с тем же operation_id', async ({ page }) => {
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }),
    adminOverview: {
      pending: [], pending_total: 0, rejected: [], city_changes: [], review: [], review_total: 0,
      recent_decisions: [], open_tasks: [], team: [], awards: [], granted: [], withdrawals: [],
      task_templates: [], role_changes: [], manual_grants: [],
    },
    taskCreateResponses: [
      { status: 503, body: { message: 'Ответ сервера не подтверждён' } },
      { status: 200, body: { ok: true, announcement_status: 'not_requested' } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.evaluate(() => {
    document.getElementById('ntTitle').value = 'Поправить парковку';
    document.getElementById('ntCity').value = 'Краснодар';
    document.getElementById('ntAddr').value = 'ул. Красная, 1';
    document.getElementById('ntReward').value = '80';
    document.getElementById('ntBudgetCap').value = '80';
    setWizardStep(3, false);
  });
  await page.locator('#ntCreate').click();
  await page.locator('#ntPublish').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/task/create'
  ).length).toBe(1);
  const first = harness.requests.find(item => item.path === '/api/admin/task/create');
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_task_create_draft')))
    .toContain(first.body.operation_id);

  await page.locator('#ntPreviewBack').click();
  await page.locator('#ntCreate').click();
  await page.locator('#ntPublish').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/task/create'
  ).length).toBe(2);
  const requests = harness.requests.filter(item => item.path === '/api/admin/task/create');
  expect(requests[1].body.operation_id).toBe(requests[0].body.operation_id);
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_task_create_draft'))).toBeNull();
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

test('ручная сверка объясняет причину и требует аудиторский комментарий', async ({ page }) => {
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }),
    adminOverview: {
      pending: [], pending_total: 0, rejected: [], review: [], review_total: 0,
      team: [], awards: [], granted: [], withdrawals: [], open_tasks: [],
      task_templates: [], pending_dispute_total: 1,
      recent_decisions: [{
        id: 91, assignment_id: 81, dispute_id: 71, title: 'Поправить парковку',
        type_title: 'Парковка', emoji: '🚲', reward: 100, status: 'closed',
        city: 'Краснодар', address: 'ТЦ Центр', claimed_name: 'Иван',
        dispute_status: 'manual_required', can_decide_dispute: true,
        dispute_reason: 'Фото относится к другой парковке',
        dispute_reconciliation_reason: 'Свободный баланс ниже суммы спора.',
        dispute_opened_at: '2026-07-28T12:00:00+00:00',
        dispute_opened_by_name: 'Скаут один',
      }],
    },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  const card = page.locator('#adDecisions .task').filter({ hasText: 'Поправить парковку' });
  await expect(card).toContainText('Нужна ручная сверка');
  await expect(card).toContainText('Свободный баланс ниже суммы спора');
  await card.getByRole('button', { name: 'Выплату исправили' }).click();
  await expect(page.locator('#askSheet')).toBeVisible();
  await expect(page.locator('#askTitle')).toHaveText('Номер операции или обращения');
  await page.locator('#askText').fill('BB-142');
  await page.locator('#askOk').click();
  await expect(page.locator('#askTitle')).toHaveText('Записать исправление выплаты');
  await page.locator('#askText').fill('Сверено с Бибибайком, обращение BB-142');
  await page.locator('#askOk').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/task/dispute' && item.body?.decision === 'manual_reversed'
  ).length).toBe(1);
  const request = harness.requests.find(
    item => item.path === '/api/admin/task/dispute' && item.body?.decision === 'manual_reversed'
  );
  expect(request.body.dispute_id).toBe(71);
  expect(request.body.note).toContain('BB-142');
  expect(request.body.reconciliation_reference).toBe('BB-142');
});

test('снятие награды требует причину и идемпотентную операцию', async ({ page }) => {
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }),
    adminOverview: {
      pending: [], pending_total: 0, rejected: [], review: [], review_total: 0,
      recent_decisions: [], team: [], awards: [], withdrawals: [], open_tasks: [],
      task_templates: [], granted: [{
        id: 41, user_id: 501, full_name: 'Иван', emoji: '🏅', title: 'Спас байк',
        bonus: 50, note: 'Помог ночью', granted_at: '2026-07-28T12:00:00+00:00',
      }],
    },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await page.locator('[data-awrev="41"]').click();
  await expect(page.locator('#askSheet')).toBeVisible();
  await expect(page.locator('#askLead')).toContainText('списан целиком');
  await page.locator('#askText').fill('Награда выдана не тому участнику');
  await page.locator('#askOk').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/award/revoke'
  ).length).toBe(1);
  const request = harness.requests.find(item => item.path === '/api/admin/award/revoke');
  expect(request.body.entry_id).toBe(41);
  expect(request.body.note).toContain('не тому');
  expect(request.body.operation_id).toMatch(/^[0-9a-f-]{36}$/i);
});

test('award grant has final confirmation and retries with the same operation id', async ({ page }) => {
  const overview = {
    pending: [], pending_total: 0, rejected: [], city_changes: [], review: [],
    review_total: 0, recent_decisions: [], open_tasks: [], granted: [], withdrawals: [],
    task_templates: [], role_changes: [],
    team: [{ user_id: 501, name: 'Иван Петров', status: 'approved', role: 'helper' }],
    awards: [{ id: 41, emoji: '🏅', title: 'Спас байк', description: 'Помощь', bonus: 50, repeatable: false, active: true }],
  };
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
    awardGrantResponses: [
      { status: 503, body: { message: 'Ответ сервера не подтверждён' } },
      { status: 200, body: { ok: true, bonus: 50 } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await page.locator('[data-awgrant="41"]').click();
  await page.locator('#grantSave').click();
  await expect(page.locator('#grantSheet')).toBeVisible();
  expect(harness.requests.filter(item => item.path === '/api/admin/award/grant')).toHaveLength(0);
  await page.locator('#grantNote').fill('Поправил парковку и прислал фото');
  await page.locator('#grantSave').click();
  await expect(page.locator('#awardGrantConfirmSheet')).toBeVisible();
  await expect(page.locator('#awardGrantConfirmBody')).toContainText('Иван Петров');
  await expect(page.locator('#awardGrantConfirmBody')).toContainText('+50⚡');
  expect(harness.requests.filter(item => item.path === '/api/admin/award/grant')).toHaveLength(0);
  await page.locator('#awardGrantConfirm').click();
  await expect.poll(() => harness.requests.filter(item => item.path === '/api/admin/award/grant').length).toBe(1);
  const first = harness.requests.find(item => item.path === '/api/admin/award/grant');
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_award_grant_501_41'))).toContain(first.body.operation_id);
  await page.locator('#awardGrantConfirm').click();
  await expect.poll(() => harness.requests.filter(item => item.path === '/api/admin/award/grant').length).toBe(2);
  const grants = harness.requests.filter(item => item.path === '/api/admin/award/grant');
  expect(grants[1].body.operation_id).toBe(grants[0].body.operation_id);
  await expect(page.locator('#awardGrantConfirmSheet')).toBeHidden();
});

test('definitive award grant error returns to a working form', async ({ page }) => {
  const overview = {
    pending: [], pending_total: 0, rejected: [], city_changes: [], review: [], review_total: 0,
    recent_decisions: [], open_tasks: [], granted: [], withdrawals: [], task_templates: [],
    role_changes: [], manual_grants: [],
    team: [{ user_id: 501, name: 'Иван Петров', status: 'approved', role: 'helper' }],
    awards: [{ id: 41, emoji: '🏅', title: 'Спас байк', bonus: 50, repeatable: false, active: true }],
  };
  await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
    awardGrantResponses: [{ status: 409, body: { message: 'Награда уже выдана.' } }],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await page.locator('[data-awgrant="41"]').click();
  await page.locator('#grantNote').fill('Поправил парковку и прислал фото');
  await page.locator('#grantSave').click();
  await page.locator('#awardGrantConfirm').click();
  await expect(page.locator('#awardGrantConfirmSheet')).toBeHidden();
  await expect(page.locator('#grantSheet')).toBeVisible();
  await expect(page.locator('#grantSave')).toBeEnabled();
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_award_grant_501_41'))).toBeNull();
});

test('manual grant has final confirmation and retries with the same operation id', async ({ page }) => {
  const overview = {
    pending: [], pending_total: 0, rejected: [], city_changes: [], review: [],
    review_total: 0, recent_decisions: [], open_tasks: [], awards: [], granted: [],
    withdrawals: [], task_templates: [], role_changes: [], manual_grants: [],
    team: [{
      user_id: 501, name: 'Иван Петров', status: 'approved', role: 'helper',
      bonus: 20, done_count: 2, trust_emoji: '🌱', trust_name: 'Новичок', tags: [],
    }],
  };
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
    manualGrantResponses: [
      { status: 503, body: { message: 'Ответ сервера не подтверждён' } },
      { status: 200, body: { ok: true, balance: 100 } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubTeam"]').click();
  await page.locator('[data-member="501"]').first().click();
  await page.locator('#msPlus').click();
  await page.locator('#balanceAmount').fill('80');
  await page.locator('#balanceReason').fill('Поправил парковку и прислал фото');
  await page.locator('#balanceSave').click();
  await expect(page.locator('#balanceConfirmSheet')).toBeVisible();
  await expect(page.locator('#balanceConfirmBody')).toContainText('Иван Петров');
  await expect(page.locator('#balanceConfirmBody')).toContainText('+80⚡');
  expect(harness.requests.filter(item => item.path === '/api/admin/grant')).toHaveLength(0);

  await page.locator('#balanceConfirm').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/grant'
  ).length).toBe(1);
  const first = harness.requests.find(item => item.path === '/api/admin/grant');
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_manual_grant_501')))
    .toContain(first.body.operation_id);
  await expect(page.locator('#balanceConfirmSheet')).toBeVisible();
  await expect(page.locator('#toast')).toContainText('Ответ не подтверждён');

  await page.locator('#balanceConfirm').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/grant'
  ).length).toBe(2);
  const grants = harness.requests.filter(item => item.path === '/api/admin/grant');
  expect(grants[1].body.operation_id).toBe(grants[0].body.operation_id);
  await expect(page.locator('#balanceConfirmSheet')).toBeHidden();
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_manual_grant_501'))).toBeNull();
});

test('manual grant correction requires final confirmation and retries safely', async ({ page }) => {
  const overview = {
    pending: [], pending_total: 0, rejected: [], city_changes: [], review: [],
    review_total: 0, recent_decisions: [], open_tasks: [], team: [], awards: [],
    granted: [], withdrawals: [], task_templates: [], role_changes: [],
    manual_grants: [{
      operation_id: 'grant-operation-501', user_id: 501, full_name: 'Иван Петров',
      amount: 80, reason: 'Поправил парковку', created_at: '2026-07-28T12:00:00+00:00',
      maker_id: 201, maker_name: 'Скаут Мария', requested_by: 202,
      requester_name: 'Скаут Олег', decided_by: 203, checker_name: 'Скаут Елена',
      can_request_reversal: true, can_decide_reversal: false,
    }],
  };
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
    manualReversalResponses: [
      { status: 503, body: { message: 'Ответ сервера не подтверждён' } },
      { status: 200, body: { ok: true, reversal_id: 71, status: 'pending' } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await expect(page.locator('#manualGrantHistory')).toContainText('Начислил: Скаут Мария');
  await expect(page.locator('#manualGrantHistory')).toContainText('Исправление запросил: Скаут Олег');
  await expect(page.locator('#manualGrantHistory')).toContainText('Проверил: Скаут Елена');
  await page.locator('[data-mg-request="grant-operation-501"]').click();
  await page.locator('#askText').fill('Начисление отправлено не за тот фотоотчёт');
  await page.locator('#askOk').click();
  await expect(page.locator('#manualCorrectionConfirmSheet')).toBeVisible();
  await expect(page.locator('#manualCorrectionConfirmBody')).toContainText('Полное сторно: 80⚡');
  expect(harness.requests.filter(item => item.path === '/api/admin/grant/reversal')).toHaveLength(0);

  await page.locator('#manualCorrectionConfirm').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/grant/reversal'
  ).length).toBe(1);
  const first = harness.requests.find(item => item.path === '/api/admin/grant/reversal');
  expect(first.body.action).toBe('request');
  expect(await page.evaluate(() => sessionStorage.getItem(
    'bibitasks_manual_reversal_request_grant-operation-501'
  ))).toContain(first.body.operation_id);
  await expect(page.locator('#manualCorrectionConfirmSheet')).toBeVisible();

  await page.locator('#manualCorrectionConfirm').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/grant/reversal'
  ).length).toBe(2);
  const requests = harness.requests.filter(item => item.path === '/api/admin/grant/reversal');
  expect(requests[1].body.operation_id).toBe(requests[0].body.operation_id);
  await expect(page.locator('#manualCorrectionConfirmSheet')).toBeHidden();
});

test('definitive correction conflict closes confirmation and refreshes history', async ({ page }) => {
  const overview = {
    pending: [], pending_total: 0, rejected: [], city_changes: [], review: [],
    review_total: 0, recent_decisions: [], open_tasks: [], team: [], awards: [],
    granted: [], withdrawals: [], task_templates: [], role_changes: [],
    manual_grants: [{
      operation_id: 'grant-operation-502', user_id: 502, full_name: 'Ольга',
      amount: 100, reason: 'Помощь на парковке', created_at: '2026-07-28T12:00:00+00:00',
      reversal_id: 72, reversal_status: 'manual_required',
      can_request_reversal: false, can_decide_reversal: true,
    }],
  };
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
    manualReversalResponses: [{
      status: 409,
      body: { error: 'manual_required', message: 'Для полного сторно недостаточно баланса.' },
    }],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await page.locator('[data-mg-decide="72"][data-mg-decision="approve"]').click();
  await page.locator('#askText').fill('Проверены проводка и получатель');
  await page.locator('#askOk').click();
  await page.locator('#manualCorrectionConfirm').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/grant/reversal'
  ).length).toBe(1);
  await expect(page.locator('#manualCorrectionConfirmSheet')).toBeHidden();
  expect(await page.evaluate(() => sessionStorage.getItem(
    'bibitasks_manual_reversal_decision_72_approve'
  ))).toBeNull();
  expect(harness.requests.filter(item => item.path === '/api/admin/overview').length)
    .toBeGreaterThanOrEqual(2);
});

test('подтверждение отчёта показывает сумму и повторяет неоднозначный ответ с тем же operation_id', async ({ page }) => {
  const overview = {
    pending: [], pending_total: 0, rejected: [], city_changes: [],
    review_total: 1, recent_decisions: [], open_tasks: [], team: [],
    awards: [], granted: [], withdrawals: [], task_templates: [], role_changes: [],
    review: [{
      id: 501, assignment_id: 701, title: 'Поправить парковку у вокзала',
      type_title: 'Парковка', emoji: '🚲', reward: 120, status: 'review',
      task_status: 'review', city: 'Краснодар', address: 'Вокзал',
      claimed_name: 'Иван Петров', can_approve: true, evidence_policy: 'after_required',
      after_photos: ['https://example.test/after.jpg'],
    }],
  };
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
    approveResponses: [
      { status: 503, body: { message: 'Ответ сервера не подтверждён' } },
      { status: 200, body: { ok: true, status: 'done' } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-tok="501"]').click();
  await expect(page.locator('#approveSheet')).toBeVisible();
  await expect(page.locator('#approveBody')).toContainText('Иван Петров');
  await expect(page.locator('#approveBody')).toContainText('+120⚡');
  expect(harness.requests.filter(item => item.path === '/api/admin/task/approve')).toHaveLength(0);

  await page.locator('#approveConfirm').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/task/approve'
  ).length).toBe(1);
  const first = harness.requests.find(item => item.path === '/api/admin/task/approve');
  const stored = await page.evaluate(() => sessionStorage.getItem('bibitasks_review_approve_701'));
  expect(stored).toContain(first.body.operation_id);

  await page.locator('#approveConfirm').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/task/approve'
  ).length).toBe(2);
  const approvals = harness.requests.filter(item => item.path === '/api/admin/task/approve');
  expect(approvals[1].body.operation_id).toBe(approvals[0].body.operation_id);
  await expect(page.locator('#approveSheet')).toBeHidden();
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_review_approve_701'))).toBeNull();
});

test('роль ответственного ставится в очередь и подтверждается вторым админом', async ({ page }) => {
  const overview = {
    pending: [], pending_total: 0, rejected: [], city_changes: [], review: [],
    review_total: 0, recent_decisions: [], open_tasks: [], team: [], awards: [],
    granted: [], withdrawals: [], task_templates: [],
    role_changes: [{
      id: 91, user_id: 303, user_name: 'Новый скаут', from_role: 'helper',
      to_role: 'admin', reason: 'Будет проверять задания вечерней смены',
      requested_by: 202, requested_by_name: 'Скаут один',
      requested_at: '2026-07-28T12:00:00+00:00', can_decide: true,
    }],
  };
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubTeam"]').click();
  const queue = page.locator('#adRoleChanges');
  await expect(queue).toContainText('Новый скаут');
  await expect(queue).toContainText('Помощник → Ответственный');
  await queue.getByRole('button', { name: 'Подтвердить' }).click();
  await expect(page.locator('#askSheet')).toBeVisible();
  await expect(page.locator('#askLead')).toContainText('личность участника');
  await page.locator('#askText').fill('Личность и доступ проверены');
  await page.locator('#askOk').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/role' && item.body?.action === 'decide'
  ).length).toBe(1);
  const decision = harness.requests.find(
    item => item.path === '/api/admin/role' && item.body?.action === 'decide'
  );
  expect(decision.body.change_id).toBe(91);
  expect(decision.body.decision).toBe('approve');
  expect(decision.body.operation_id).toMatch(/^[0-9a-f-]{36}$/i);

  await page.evaluate(() => openRole(303, 'Новый скаут', 'helper'));
  await page.locator('#roleSheet [data-role="admin"]').click();
  await expect(page.locator('#askTitle')).toHaveText('Запросить доступ ответственного');
  await page.locator('#askText').fill('Будет проверять задания вечерней смены');
  await page.locator('#askOk').click();
  await expect.poll(() => harness.requests.filter(
    item => item.path === '/api/admin/role' && item.body?.action === 'request'
  ).length).toBe(1);
  const request = harness.requests.find(
    item => item.path === '/api/admin/role' && item.body?.action === 'request'
  );
  expect(request.body.user_id).toBe(303);
  expect(request.body.reason).toContain('вечерней смены');
  expect(request.body.operation_id).toMatch(/^[0-9a-f-]{36}$/i);
});
