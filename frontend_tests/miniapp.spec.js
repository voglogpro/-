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

function staffState(capabilities, overrides = {}) {
  return state({
    ...overrides,
    is_admin: true,
    staff_access: {
      policy_version: 1,
      presets: overrides.presets || [],
      capabilities,
    },
  });
}

function emptyAdminOverview(overrides = {}) {
  return {
    pending: [], pending_total: 0, rejected: [], city_changes: [], review: [],
    review_total: 0, recent_decisions: [], open_tasks: [], team: [], awards: [],
    granted: [], withdrawals: [], task_templates: [], role_changes: [],
    manual_grants: [], join_requests: [], award_reversals: { open: [], history: [] }, ...overrides,
  };
}

const SCOUT_CAPABILITIES = [
  'application.queue.view', 'application.review', 'admission.view', 'admission.retry',
  'member.search', 'member.tags.view', 'member.tags.manage', 'member.city.review',
  'member.role.manage_basic', 'task.view', 'task.create', 'task.cancel',
  'task.delivery.view', 'task.template.manage', 'telegram.publication.manage',
];
const REVIEWER_CAPABILITIES = [
  'task.review.queue', 'task.review', 'task.dispute.request', 'task.dispute.decide',
  'bonus.grant.small', 'bonus.reversal.request', 'bonus.reversal.decide',
  'award.view', 'award.grant', 'award.revoke', 'award.reversal.request',
  'award.reversal.decide', 'member.task_summary.view',
];
const CASHIER_CAPABILITIES = [
  'withdrawal.queue.view', 'withdrawal.account.reveal', 'withdrawal.handoff',
  'withdrawal.decide', 'member.financial_summary.view',
];
const OWNER_CAPABILITIES = Array.from(new Set([
  ...SCOUT_CAPABILITIES, ...REVIEWER_CAPABILITIES, ...CASHIER_CAPABILITIES,
  'access.view', 'access.request', 'access.decide', 'award.catalog.manage',
  'telegram.inbox.redrive', 'operations.health.view',
]));

const TEMPLATE_ID = '11111111-1111-4111-8111-111111111111';
const TEMPLATE_VERSION_ID = '71111111-1111-4111-8111-111111111111';
function taskTemplate(overrides = {}) {
  return {
    id: TEMPLATE_ID, key: 'parking', generation: 5, version_id: TEMPLATE_VERSION_ID, version_number: 3,
    status: 'active', title: 'Парковка у ТЦ', task_title: 'Поправить парковку байков',
    type: 'fix_zone', details: 'Выровнять байки и освободить проход', reward: 80,
    mode: 'open', evidence_policy: 'after_required', max_participants: 1,
    budget_cap: 80, photo_url: 'https://example.test/template-parking.jpg',
    ...overrides,
  };
}

const TEMPLATE_TASK_TYPES = [
  { key: 'fix_zone', title: 'Парковка', emoji: '🚲' },
  { key: 'photo_check', title: 'Фото-проверка', emoji: '📷' },
];

async function openMiniApp(page, options = {}) {
  let currentState = options.initialState || state();
  const requests = [];
  let announcementStatusCall = 0;
  let approveCall = 0;
  let awardGrantCall = 0;
  let manualGrantCall = 0;
  let manualReversalCall = 0;
  let awardReversalCall = 0;
  let applyCall = 0;
  let memberSearchCall = 0;
  let taskCreateCall = 0;
  let templateWriteCall = 0;
  let stateCall = 0;
  let lastAdminOverview = null;
  const templateStore = options.templateStore
    ? JSON.parse(JSON.stringify(options.templateStore))
    : { active: [], archived: [] };

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
    if (url.pathname === '/api/admin/access' && options.accessData) {
      if (request.method() === 'POST') {
        const value = typeof options.accessPostResponse === 'function'
          ? options.accessPostResponse(body)
          : (options.accessPostResponse || { ok: true });
        return route.fulfill({
          status: value.status || 200,
          contentType: 'application/json',
          body: JSON.stringify(value.body || value),
        });
      }
      const value = typeof options.accessData === 'function'
        ? options.accessData()
        : options.accessData;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(value),
      });
    }
    if (url.pathname === '/api/admin/task-templates' && request.method() === 'GET' && options.templateStore) {
      const status = url.searchParams.get('status') || 'active';
      const allItems = status === 'all'
        ? [...(templateStore.active || []), ...(templateStore.archived || [])]
        : (templateStore[status] || []);
      const afterId = url.searchParams.get('after_id');
      const afterIndex = afterId == null ? -1 : allItems.findIndex(item => String(item.id) === afterId);
      const start = afterId == null ? 0 : afterIndex + 1;
      const requestedLimit = Math.max(1, Number(url.searchParams.get('limit')) || 50);
      const pageSize = Math.min(requestedLimit, options.templatePageSize || requestedLimit);
      const items = allItems.slice(start, start + pageSize);
      const nextCursor = start + items.length < allItems.length && items.length
        ? String(items[items.length - 1].id) : null;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items, next_cursor: nextCursor }),
      });
    }
    const templateDetail = url.pathname.match(/^\/api\/admin\/task-templates\/([^/]+)$/);
    if (templateDetail && request.method() === 'GET' && options.templateStore) {
      const id = decodeURIComponent(templateDetail[1]);
      const delay = Number((options.templateDetailDelays || {})[id] || 0);
      if (delay > 0) await new Promise(resolve => setTimeout(resolve, delay));
      const item = [...(templateStore.active || []), ...(templateStore.archived || [])]
        .find(value => String(value.id) === id);
      return route.fulfill({
        status: item ? 200 : 404,
        contentType: 'application/json',
        body: JSON.stringify(item ? { template: item } : { message: 'Шаблон не найден' }),
      });
    }
    const templateVersion = url.pathname.match(/^\/api\/admin\/task-templates\/([^/]+)\/versions$/);
    const templateStatus = url.pathname.match(/^\/api\/admin\/task-templates\/([^/]+)\/status$/);
    const templateCreate = url.pathname === '/api/admin/task-templates' && request.method() === 'POST';
    if ((templateCreate || templateVersion || templateStatus) && options.templateStore) {
      const sequence = options.templateWriteResponses || [];
      const configured = sequence.length
        ? sequence[Math.min(templateWriteCall, sequence.length - 1)]
        : { status: 200, body: { ok: true } };
      templateWriteCall += 1;
      if ((configured.status || 200) < 400) {
        if (templateCreate) {
          const id = `99999999-9999-4999-8999-${String(templateWriteCall).padStart(12, '0')}`;
          templateStore.active.push({
            ...body, id, generation: 1,
            version_id: `89999999-9999-4999-8999-${String(templateWriteCall).padStart(12, '0')}`,
            version_number: 1,
            status: 'active', photo_url: body.photo_action === 'replace' ? 'https://example.test/template-new.jpg' : '',
          });
        } else if (templateVersion) {
          const id = decodeURIComponent(templateVersion[1]);
          const item = [...templateStore.active, ...templateStore.archived].find(value => String(value.id) === id);
          if (item) Object.assign(item, body, {
            generation: Number(item.generation || 0) + 1,
            version_id: `79999999-9999-4999-8999-${String(templateWriteCall).padStart(12, '0')}`,
            version_number: Number(item.version_number || 0) + 1,
            photo_url: body.photo_action === 'remove' ? '' :
              body.photo_action === 'replace' ? 'https://example.test/template-updated.jpg' : item.photo_url,
          });
        } else if (templateStatus) {
          const id = decodeURIComponent(templateStatus[1]);
          const from = body.status === 'active' ? templateStore.archived : templateStore.active;
          const to = body.status === 'active' ? templateStore.active : templateStore.archived;
          const index = from.findIndex(item => String(item.id) === id);
          if (index >= 0) to.push({ ...from.splice(index, 1)[0], status: body.status, generation: Number(body.expected_generation || 0) + 1 });
        }
      }
      return route.fulfill({
        status: configured.status || 200,
        contentType: 'application/json',
        body: JSON.stringify(configured.body || { ok: true }),
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
    if (url.pathname === '/api/admin/award/reversal' && options.awardReversalResponses) {
      const sequence = options.awardReversalResponses;
      const value = sequence[Math.min(awardReversalCall, sequence.length - 1)];
      awardReversalCall += 1;
      if (typeof value.commit === 'function') value.commit(body);
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
  return { requests, templateStore, setState(value) { currentState = value; } };
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
  await expect(page.locator('#nav')).toBeHidden();
  await expect(page.locator('label[for="apAbout"]')).toContainText('обязательно');
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
  await expect(page.locator('#waitMessage')).toContainText('72 часов');
  await expect(page.locator('#nav')).toBeHidden();
  await expect(page.locator('#waitMessage')).not.toContainText('до одного дня');
});

test('реферальный новичок проходит вступление до анкеты', async ({ page }) => {
  const initial = state({
    referral_gate: {
      required: true, invited: true, confirmed: false,
      url: 'https://t.me/+join-request',
    },
  });
  const harness = await openMiniApp(page, { initialState: initial });

  await expect(page.locator('#subBox')).toBeVisible();
  await expect(page.locator('#subBox')).toContainText('Шаг 1 · вступление');
  await expect(page.locator('#subCheck')).toHaveText('Проверить вступление');
  await expect(page.locator('#applyBox')).toBeHidden();
  await expect(page.locator('#nav')).toBeHidden();

  harness.setState(state({
    referral_gate: { ...initial.referral_gate, confirmed: true },
  }));
  await page.evaluate(() => load());
  await expect(page.locator('#subBox')).toBeHidden();
  await expect(page.locator('#applyBox')).toBeVisible();
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
  await expect(page.locator('#nav')).toBeHidden();
  harness.setState(state({ can_work: true, me: { applied: true, status: 'approved' } }));
  await page.locator('#waitRefresh').click();
  await expect(page.locator('#worksBox')).toBeVisible();
  await expect(page.locator('#waitBox')).toBeHidden();
  await expect(page.locator('#nav')).toBeVisible();
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

test('на мобильной ширине 375–390px рабочие формы складываются в одну колонку', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openMiniApp(page);
  await page.evaluate(() => {
    document.getElementById('tab-admin').classList.remove('hidden');
    document.getElementById('adSubCreate').classList.remove('hidden');
    setWizardStep(2, false);
  });
  const fields = page.locator('.wizard-step[data-wstep="2"] .two').first();
  for (const width of [375, 390]) {
    await page.setViewportSize({ width, height: 844 });
    const columns = await fields.evaluate(
      element => getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/)
    );
    expect(columns).toHaveLength(1);
  }
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

test('скаут различает этапы вступления и безопасно повторяет решение Telegram', async ({ page }) => {
  const requestKey = 'b'.repeat(64);
  const overview = {
    pending: [], pending_total: 0, rejected: [], review: [], review_total: 0,
    team: [], awards: [], granted: [], withdrawals: [], open_tasks: [],
    task_templates: [], recent_decisions: [], role_changes: [], manual_grants: [],
    join_requests: [{
      request_key: requestKey, user_id: 202, full_name: 'Иван',
      username: 'ivan', city: 'Краснодар', source: 'bot_invite',
      status: 'manual_required', decision: 'approve',
      requested_at: '2026-07-28T09:00:00+00:00', last_error: 'TelegramBadRequest',
    }],
  };
  const harness = await openMiniApp(page, {
    initialState: state({ is_admin: true }), adminOverview: overview,
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();

  await expect(page.locator('#adJoinRequests')).toContainText('Нужно внимание');
  await expect(page.locator('#adJoinRequests')).toContainText('TelegramBadRequest');
  await page.getByRole('button', { name: 'Повторить одобрение' }).click();
  await page.locator('#askText').fill('Проверил заявку в Telegram');
  await page.locator('#askOk').click();

  await expect.poll(() => harness.requests.some(item =>
    item.path === '/api/admin/join-request/retry'
    && item.body.request_key === requestKey
    && item.body.decision === 'approve'
    && item.body.reason === 'Проверил заявку в Telegram'
    && /^[0-9a-f-]{36}$/.test(item.body.operation_id)
  )).toBe(true);
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
  await openMiniApp(page, {
    walletFailure: true,
    initialState: state({ can_work: true, me: { status: 'approved' } }),
  });
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
  await expect.poll(() => page.evaluate(
    () => sessionStorage.getItem('bibitasks_task_create_draft')
  )).toBeNull();
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

test('исправление награды создаёт запрос только после сводки и безопасно повторяет operation_id', async ({ page }) => {
  const overview = emptyAdminOverview({ granted: [{
    id: 41, user_id: 501, full_name: 'Иван', emoji: '🏅', title: 'Спас байк',
    bonus: 50, note: 'Помог ночью', granted_at: '2026-07-28T12:00:00+00:00',
    granter_name: 'Скаут Мария', can_request_reversal: true,
  }] });
  const harness = await openMiniApp(page, {
    initialState: staffState(['award.view', 'award.reversal.request']),
    adminOverview: overview,
    awardReversalResponses: [
      { status: 503, body: { message: 'Ответ сервера не подтверждён' } },
      { status: 200, body: { ok: true, reversal_id: 71, status: 'pending' } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  const requestButton = page.getByRole('button', {
    name: 'Запросить исправление награды «Спас байк» для Иван',
  });
  await expect(requestButton).toHaveText('Исправить выдачу');
  await requestButton.click();
  await expect(page.locator('#askLead')).toContainText('не изменятся');
  await page.locator('#askText').fill('Награда выдана не тому участнику');
  await page.locator('#askOk').click();
  await expect(page.locator('#awardReversalConfirmSheet')).toBeVisible();
  await expect(page.locator('#awardReversalConfirmBody')).toContainText('Запросить полное сторно: 50⚡');
  expect(harness.requests.filter(item => item.path === '/api/admin/award/reversal')).toHaveLength(0);

  await page.locator('#awardReversalConfirm').click();
  await expect(page.locator('#awardReversalConfirmError')).toContainText('номер операции сохранён');
  await expect(page.locator('#awardReversalConfirmSheet')).toBeVisible();
  const storage = await page.evaluate(() => sessionStorage.getItem('bibitasks_award_reversal_request_41'));
  const first = harness.requests.find(item => item.path === '/api/admin/award/reversal');
  expect(storage).toContain(first.body.operation_id);

  await page.locator('#awardReversalConfirm').click();
  await expect(page.locator('#awardReversalConfirmSheet')).toBeHidden();
  const requests = harness.requests.filter(item => item.path === '/api/admin/award/reversal');
  expect(requests).toHaveLength(2);
  expect(requests[0].body).toMatchObject({
    action: 'request', entry_id: 41, reason: 'Награда выдана не тому участнику',
  });
  expect(requests[1].body.operation_id).toBe(requests[0].body.operation_id);
  expect(harness.requests.filter(item => item.path === '/api/admin/award/revoke')).toHaveLength(0);
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_award_reversal_request_41'))).toBeNull();
});

test('второй ответственный подтверждает полное сторно и повторяет решение с отдельным operation_id', async ({ page }) => {
  const correction = {
    id: 72, member_award_id: 42, status: 'pending', user_id: 502, full_name: 'Ольга',
    award_title: 'Спасла парковку', emoji: '🏅', amount: 80, original_note: 'Ночная помощь',
    granted_at: '2026-07-27T10:00:00+00:00', granter_name: 'Скаут Мария',
    reason: 'Выдали не по тому отчёту', requested_by: 201, requester_name: 'Скаут Олег',
    requested_at: '2026-07-28T12:00:00+00:00', current_balance: 120,
    available_balance: 100, reserved_amount: 20, deficit: 0, can_decide: true,
  };
  const harness = await openMiniApp(page, {
    initialState: staffState(['award.view', 'award.reversal.decide']),
    adminOverview: emptyAdminOverview({ award_reversals: { open: [correction], history: [] } }),
    awardReversalResponses: [
      { status: 503, body: { message: 'Неопределённый ответ' } },
      { status: 200, body: { ok: true, reversal_id: 72, status: 'applied', balance: 40 } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await page.getByRole('button', { name: /Подтвердить полное сторно награды «Спасла парковку»/ }).click();
  await page.locator('#askText').fill('Проверены получатель, выдача и исходная проводка');
  await page.locator('#askOk').click();
  await expect(page.locator('#awardReversalConfirmBody')).toContainText('Списать полностью: 80⚡');
  await page.locator('#awardReversalConfirm').click();
  await expect(page.locator('#awardReversalConfirmError')).toContainText('номер операции сохранён');
  const stored = await page.evaluate(() => sessionStorage.getItem('bibitasks_award_reversal_decision_72_approve'));
  const first = harness.requests.find(item => item.path === '/api/admin/award/reversal');
  expect(stored).toContain(first.body.operation_id);
  await page.locator('#awardReversalConfirm').click();
  await expect(page.locator('#awardReversalConfirmSheet')).toBeHidden();
  const requests = harness.requests.filter(item => item.path === '/api/admin/award/reversal');
  expect(requests[0].body).toMatchObject({ action: 'decide', reversal_id: 72, decision: 'approve' });
  expect(requests[1].body.operation_id).toBe(requests[0].body.operation_id);
  expect(requests[0].body.operation_id).not.toBe('grant-operation-501');
});

test('второй ответственный может оставить награду без изменения баланса', async ({ page }) => {
  const correction = {
    id: 73, member_award_id: 43, status: 'pending', user_id: 503, full_name: 'Илья',
    award_title: 'Помощь новичку', emoji: '🤝', amount: 30, reason: 'Нужна повторная проверка',
    requested_by: 201, requester_name: 'Скаут Олег', requested_at: '2026-07-28T12:00:00+00:00',
    can_decide: true,
  };
  const harness = await openMiniApp(page, {
    initialState: staffState(['award.view', 'award.reversal.decide']),
    adminOverview: emptyAdminOverview({ award_reversals: { open: [correction], history: [] } }),
    awardReversalResponses: [{ status: 200, body: { ok: true, reversal_id: 73, status: 'rejected' } }],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await page.getByRole('button', { name: /Оставить награду «Помощь новичку»/ }).click();
  await page.locator('#askText').fill('Исходная выдача подтверждена фотоотчётом');
  await page.locator('#askOk').click();
  await expect(page.locator('#awardReversalConfirmBody')).toContainText('Оставить: 30⚡');
  await page.locator('#awardReversalConfirm').click();
  const request = harness.requests.find(item => item.path === '/api/admin/award/reversal');
  expect(request.body).toMatchObject({ action: 'decide', reversal_id: 73, decision: 'reject' });
});

test('недостаточный баланс переводит запрос в постоянную ручную сверку без частичного списания', async ({ page }) => {
  const pending = {
    id: 78, member_award_id: 48, status: 'pending', user_id: 508, full_name: 'Пётр',
    award_title: 'Помощь на парковке', emoji: '🚲', amount: 100, reason: 'Дублирующая выдача',
    requested_by: 201, requester_name: 'Скаут Олег', requested_at: '2026-07-28T12:00:00+00:00',
    current_balance: 100, available_balance: 100, deficit: 0, can_decide: true,
  };
  const manual = {
    ...pending, status: 'manual_required', current_balance: 35, available_balance: 20,
    reserved_amount: 15, deficit: 80, can_approve: false, can_reject: true,
    approve_block_reason: 'Недостаточно свободного баланса для полного сторно.',
    manual_reason: 'Незарезервированного баланса недостаточно.',
    wait_reason: 'Пополните баланс или завершите внешнюю сверку.',
  };
  let manualMode = false;
  const harness = await openMiniApp(page, {
    initialState: staffState(['award.view', 'award.reversal.decide']),
    adminOverview: () => emptyAdminOverview({
      award_reversals: { open: [manualMode ? manual : pending], history: [] },
    }),
    awardReversalResponses: [{
      status: 409,
      body: { error: 'manual_required', message: 'Для полного сторно недостаточно баланса.' },
      commit: () => { manualMode = true; },
    }],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await page.getByRole('button', { name: /Подтвердить полное сторно награды «Помощь на парковке»/ }).click();
  await page.locator('#askText').fill('Проверены выдача и доступный баланс');
  await page.locator('#askOk').click();
  await page.locator('#awardReversalConfirm').click();
  await expect(page.locator('#awardReversalConfirmSheet')).toBeHidden();
  await expect(page.locator('#awReversalOpen')).toContainText('Не хватает 80⚡');
  await expect(page.locator('#awReversalOpen').getByRole('alert')).toContainText('Частичного списания не будет');
  await expect(page.getByRole('button', { name: /Оставить награду «Помощь на парковке»/ })).toBeVisible();
  await expect(page.locator('#awReversalOpen').getByRole('button', { name: 'Полное сторно недоступно' })).toBeDisabled();
  expect(harness.requests.find(item => item.path === '/api/admin/award/reversal').body)
    .toMatchObject({ action: 'decide', reversal_id: 78, decision: 'approve' });
  expect(await page.evaluate(() => sessionStorage.getItem('bibitasks_award_reversal_decision_78_approve'))).toBeNull();
});

test('ручная сверка остаётся видимой, а поиск и статус фильтруют полную историю', async ({ page }) => {
  const manual = {
    id: 74, member_award_id: 44, status: 'manual_required', user_id: 504, full_name: 'Анна',
    award_title: 'Ночная помощь', emoji: '🌙', amount: 100, reason: 'Дублирующая выдача',
    requested_by: 201, requester_name: 'Скаут Олег', requested_at: '2026-07-28T12:00:00+00:00',
    current_balance: 35, available_balance: 20, reserved_amount: 15, deficit: 80,
    manual_reason: 'Незарезервированного баланса недостаточно.', can_approve: false, can_reject: true,
    approve_block_reason: 'Недостаточно свободного баланса.',
    wait_reason: 'Ожидается другой ответственный',
  };
  const applied = { ...manual, id: 75, status: 'applied', full_name: 'Борис', award_title: 'Спас байк', deficit: 0,
    decided_by: 202, checker_name: 'Скаут Елена', decision_note: 'Проводка проверена', result_balance: 40 };
  const rejected = { ...manual, id: 76, status: 'rejected', full_name: 'Вера', award_title: 'Помощь новичку', deficit: 0,
    decided_by: 203, checker_name: 'Скаут Ирина', decision_note: 'Выдача верна' };
  await openMiniApp(page, {
    initialState: staffState(['award.view', 'award.reversal.request', 'award.reversal.decide']),
    adminOverview: emptyAdminOverview({
      granted: [{ id: 44, user_id: 504, full_name: 'Анна', title: 'Ночная помощь', emoji: '🌙', bonus: 100,
        granted_at: '2026-07-27T10:00:00+00:00', reversal_status: 'applied', can_request_reversal: false }],
      award_reversals: {
        open: [manual], history: [applied, rejected], history_limit: 100,
        history_total: 245, history_truncated: true,
      },
    }),
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  await expect(page.locator('#awReversalOpen')).toContainText('Не хватает 80⚡');
  await expect(page.locator('#awReversalOpen')).toContainText('Запросил: Скаут Олег');
  await expect(page.locator('#awReversalOpen')).toContainText('Полное сторно недоступно');
  await expect(page.getByRole('button', { name: /Оставить награду «Ночная помощь»/ })).toBeVisible();
  await expect(page.locator('#awGranted')).toContainText('Исправлено');
  await expect(page.locator('#awGranted').getByRole('button', { name: /Запросить исправление/ })).toHaveCount(0);

  await page.locator('#awReversalSearch').fill('Борис');
  await expect(page.locator('#awReversalHistory')).toContainText('Спас байк');
  await expect(page.locator('#awReversalHistory')).not.toContainText('Помощь новичку');
  await page.locator('#awReversalSearch').fill('');
  await page.locator('#awReversalStatus').selectOption('rejected');
  await expect(page.locator('#awReversalHistory')).toContainText('Вера');
  await expect(page.locator('#awReversalHistory')).not.toContainText('Борис');
  await expect(page.locator('#awReversalMeta')).toContainText('Поиск по последним 100 из 245 записей истории');
  await expect(page.locator('#awReversalMeta')).toContainText('найдено: 1');
});

test('split-права независимо показывают approve и reject при отозванных полномочиях автора', async ({ page }) => {
  const revokedRequester = {
    id: 79, member_award_id: 49, status: 'pending', user_id: 509, full_name: 'Роман',
    award_title: 'Спас байк', emoji: '🏅', amount: 60, reason: 'Проверка дубля',
    requested_by: 101, requester_name: 'Текущий ответственный', requested_at: '2026-07-28T12:00:00+00:00',
    can_approve: false, can_reject: true,
    approve_block_reason: 'Полномочие автора запроса отозвано.',
  };
  const approveOnly = {
    id: 80, member_award_id: 50, status: 'pending', user_id: 510, full_name: 'Светлана',
    award_title: 'Помощь новичку', emoji: '🤝', amount: 20, reason: 'Проверка выдачи',
    requested_by: 202, requester_name: 'Другой ответственный', requested_at: '2026-07-28T12:10:00+00:00',
    deficit: 0, can_approve: true, can_reject: false,
    reject_block_reason: 'Закрыть запрос может только его автор.',
  };
  await openMiniApp(page, {
    initialState: staffState(['award.view', 'award.reversal.decide']),
    adminOverview: emptyAdminOverview({
      award_reversals: { open: [revokedRequester, approveOnly], history: [], history_limit: 100, history_total: 0, history_truncated: false },
    }),
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  const revokedCard = page.locator('#awReversalOpen article').filter({ hasText: 'Роман' });
  await expect(revokedCard.getByRole('button', { name: /Оставить награду «Спас байк»/ })).toBeVisible();
  await expect(revokedCard.getByRole('button', { name: 'Полное сторно недоступно' })).toBeDisabled();
  await expect(revokedCard).toContainText('Полномочие автора запроса отозвано');
  const approveCard = page.locator('#awReversalOpen article').filter({ hasText: 'Светлана' });
  await expect(approveCard.getByRole('button', { name: /Подтвердить полное сторно награды «Помощь новичку»/ })).toBeVisible();
  await expect(approveCard.getByRole('button', { name: 'Закрытие недоступно' })).toBeDisabled();
  await expect(approveCard).toContainText('Закрыть запрос может только его автор');
});

test('исправления наград доступны с клавиатуры и не создают горизонтальный скролл на 320–390px', async ({ page }) => {
  const correction = {
    id: 77, member_award_id: 45, status: 'pending', user_id: 505,
    full_name: 'Очень длинное имя участника для мобильного экрана',
    award_title: 'Очень длинное название награды за помощь на парковке', emoji: '🏅', amount: 120,
    reason: 'Проверить выдачу', requested_by: 201, requester_name: 'Скаут Олег',
    requested_at: '2026-07-28T12:00:00+00:00', can_decide: true,
  };
  await page.setViewportSize({ width: 320, height: 720 });
  await openMiniApp(page, {
    initialState: staffState(['award.view', 'award.reversal.decide']),
    adminOverview: emptyAdminOverview({ award_reversals: { open: [correction], history: [] } }),
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubAwards"]').click();
  for (const width of [320, 360, 390]) {
    await page.setViewportSize({ width, height: 760 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  }
  const approve = page.getByRole('button', { name: /Подтвердить полное сторно награды «Очень длинное название/ });
  await expect(approve).toBeVisible();
  expect(await approve.evaluate(element => element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44);
  await approve.focus();
  await page.keyboard.press('Enter');
  await page.locator('#askText').fill('Проверены получатель и исходная выдача');
  await page.locator('#askOk').click();
  const dialog = page.getByRole('dialog', { name: 'Подтвердить полное сторно' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute('aria-describedby', 'awardReversalConfirmLead');
  await expect(page.locator('#awardReversalConfirmCancel')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(approve).toBeFocused();
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

test('staff_access закрывает административную оболочку даже при старом is_admin', async ({ page }) => {
  await openMiniApp(page, { initialState: staffState([]) });
  await expect(page.locator('#nav [data-tab="tab-admin"]')).toBeHidden();
  await expect(page.locator('#nav')).toBeHidden();
});

test('legacy is_admin остаётся только fallback браузерной фикстуры', async ({ page }) => {
  await openMiniApp(page, {
    initialState: state({ is_admin: true }),
    adminOverview: emptyAdminOverview(),
  });
  await expect(page.locator('#nav [data-tab="tab-admin"]')).toBeVisible();
});

test('скаут видит заявки, задания и людей без финансовых разделов', async ({ page }) => {
  await openMiniApp(page, {
    initialState: staffState(SCOUT_CAPABILITIES),
    adminOverview: emptyAdminOverview(),
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await expect(page.locator('[data-asub="adSubQueue"]')).toBeVisible();
  await expect(page.locator('[data-asub="adSubCreate"]')).toBeVisible();
  await expect(page.locator('[data-asub="adSubTeam"]')).toBeVisible();
  await expect(page.locator('[data-asub="adSubAwards"]')).toBeHidden();
  await expect(page.locator('[data-asub="adSubAccess"]')).toBeHidden();
  await expect(page.locator('[data-cap-any="application.queue.view,application.review"]').first()).toBeVisible();
  await expect(page.locator('[data-cap-any="task.review.queue,task.review,task.dispute.request,task.dispute.decide"]').first()).toBeHidden();
  await page.locator('[data-asub="adSubTeam"]').click();
  await expect(page.locator('#adTeamSort option[value="bonus"]')).toBeHidden();
});

test('ревьюер видит проверку, сводку дел и бонусы, но не набор, создание и выплаты', async ({ page }) => {
  await openMiniApp(page, {
    initialState: staffState(REVIEWER_CAPABILITIES),
    adminOverview: emptyAdminOverview(),
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await expect(page.locator('[data-asub="adSubQueue"]')).toBeVisible();
  await expect(page.locator('[data-asub="adSubCreate"]')).toBeHidden();
  await expect(page.locator('[data-asub="adSubTeam"]')).toBeVisible();
  await expect(page.locator('[data-asub="adSubAwards"]')).toBeVisible();
  await expect(page.locator('[data-asub="adSubAccess"]')).toBeHidden();
  await expect(page.locator('#adReview').locator('..')).toBeVisible();
  await expect(page.locator('#adPending').locator('..')).toBeHidden();
  await page.locator('[data-asub="adSubAwards"]').click();
  await expect(page.locator('#manualGrantHistory').locator('..')).toBeVisible();
  await expect(page.locator('#adWithdrawals').locator('..')).toBeHidden();
});

test('кассир получает только очередь выплат без каталога наград', async ({ page }) => {
  await openMiniApp(page, {
    initialState: staffState(CASHIER_CAPABILITIES),
    adminOverview: emptyAdminOverview(),
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await expect(page.locator('[data-asub="adSubQueue"]')).toBeHidden();
  await expect(page.locator('[data-asub="adSubCreate"]')).toBeHidden();
  await expect(page.locator('[data-asub="adSubTeam"]')).toBeVisible();
  await expect(page.locator('[data-asub="adSubAwards"]')).toBeVisible();
  await page.locator('[data-asub="adSubAwards"]').click();
  await expect(page.locator('#adWithdrawals').locator('..')).toBeVisible();
  await expect(page.locator('#awList').locator('..')).toBeHidden();
  await expect(page.locator('#manualGrantHistory').locator('..')).toBeHidden();
});

test('владелец видит все рабочие разделы и управление доступом', async ({ page }) => {
  await openMiniApp(page, {
    initialState: staffState(OWNER_CAPABILITIES, {
      presets: [{ key: 'scout', title: 'Скаут' }],
    }),
    adminOverview: emptyAdminOverview(),
    accessData: { grants: [], changes: [], presets: { scout: SCOUT_CAPABILITIES } },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  for (const id of ['adSubQueue', 'adSubCreate', 'adSubTeam', 'adSubAwards', 'adSubAccess']) {
    await expect(page.locator(`[data-asub="${id}"]`)).toBeVisible();
  }
  await page.locator('[data-asub="adSubAccess"]').click();
  await expect(page.locator('#staffAccessRequestCard')).toBeVisible();
  await expect(page.locator('#staffAccessHistory')).toBeVisible();
});

test('доступ использует maker-checker, generation и идемпотентные операции', async ({ page }) => {
  const accessData = {
    presets: { scout: SCOUT_CAPABILITIES, reviewer: REVIEWER_CAPABILITIES },
    grants: [{
      user_id: 303, full_name: 'Иван Скаут', preset: 'scout', generation: 4, active: true,
    }],
    changes: [{
      id: 10, target_user_id: 303, target_name: 'Иван Скаут', change_action: 'assign',
      preset: 'reviewer', reason: 'Нужен контроль фото', status: 'pending',
      requested_by: 101, requested_by_name: 'Анна', can_decide: false,
      wait_reason: 'Нужен второй ответственный',
    }, {
      id: 11, target_user_id: 303, target_name: 'Иван Скаут', change_action: 'revoke',
      preset: 'scout', reason: 'Смена завершена', status: 'pending',
      requested_by: 202, requested_by_name: 'Олег', can_decide: true,
    }],
  };
  const harness = await openMiniApp(page, {
    initialState: staffState(['access.view', 'access.request', 'access.decide'], {
      presets: ['owner'],
    }),
    adminOverview: emptyAdminOverview(),
    accessData,
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await expect(page.locator('#staffAccessGeneration')).toContainText('4');
  await expect(page.getByRole('button', { name: 'Нужен второй ответственный' })).toBeDisabled();

  await page.locator('#staffAccessAction').selectOption('assign');
  await page.locator('#staffAccessPreset').selectOption('reviewer');
  await expect(page.locator('#staffAccessGeneration')).toContainText('0');
  await page.locator('#staffAccessReason').fill('Будет проверять фотографии вечерней смены');
  await page.locator('#staffAccessRequest').click();
  await expect.poll(() => harness.requests.filter(item =>
    item.path === '/api/admin/access' && item.method === 'POST' && item.body?.action === 'request'
  ).length).toBe(1);
  const request = harness.requests.find(item => item.path === '/api/admin/access' && item.body?.action === 'request');
  expect(request.body).toMatchObject({
    change_action: 'assign', target_user_id: 303, preset: 'reviewer', expected_generation: 0,
    reason: 'Будет проверять фотографии вечерней смены',
  });
  expect(request.body.operation_id).toMatch(/^[0-9a-f-]{36}$/i);

  await page.locator('[data-access-decide="11"][data-access-decision="approve"]').click();
  await page.locator('#askText').fill('Сотрудник и завершение смены проверены');
  await page.locator('#askOk').click();
  await expect.poll(() => harness.requests.filter(item =>
    item.path === '/api/admin/access' && item.method === 'POST' && item.body?.action === 'decide'
  ).length).toBe(1);
  const decision = harness.requests.find(item => item.path === '/api/admin/access' && item.body?.action === 'decide');
  expect(decision.body).toMatchObject({
    change_id: 11, decision: 'approve', note: 'Сотрудник и завершение смены проверены',
  });
  expect(decision.body.operation_id).toMatch(/^[0-9a-f-]{36}$/i);
});

test('библиотека шаблонов разделяет capability управления и публикации', async ({ page }) => {
  const template = taskTemplate();
  await openMiniApp(page, {
    initialState: staffState(['task.template.manage'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [template] }),
    templateStore: { active: [template], archived: [] },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await expect(page.locator('[data-taskmode="taskTemplateMode"]')).toBeVisible();
  await expect(page.locator('#ntCreate')).toBeHidden();
  await page.locator('[data-taskmode="taskTemplateMode"]').click();
  await expect(page.locator(`[data-template-card="${TEMPLATE_ID}"]`)).toBeVisible();
  await expect(page.locator(`[data-template-card="${TEMPLATE_ID}"] [data-tplapply]`)).toHaveCount(0);
  await expect(page.locator('#tplLibrary')).toHaveAttribute('aria-busy', 'false');

  const creator = await page.context().newPage();
  await openMiniApp(creator, {
    initialState: staffState(['task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview(),
  });
  await creator.locator('#nav [data-tab="tab-admin"]').click();
  await creator.locator('[data-asub="adSubCreate"]').click();
  await expect(creator.locator('[data-taskmode="taskTemplateMode"]')).toBeHidden();
  await expect(creator.locator('#ntCreate')).toBeVisible();
  await creator.close();
});

test('активный шаблон архивируется и восстанавливается с generation и operation_id', async ({ page }) => {
  const template = taskTemplate();
  const harness = await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [template] }),
    templateStore: { active: [template], archived: [] },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('[data-taskmode="taskTemplateMode"]').click();
  await page.getByRole('button', { name: 'Архивировать' }).click();
  await expect(page.locator('#tplConfirmLead')).toContainText('созданные задания не изменятся');
  await page.locator('#tplConfirmOk').click();
  await expect(page.locator('#tplLibrary')).toContainText('Шаблонов пока нет');
  const archive = harness.requests.find(item => item.path === `/api/admin/task-templates/${TEMPLATE_ID}/status`);
  expect(archive.body).toMatchObject({ status: 'archived', expected_generation: 5 });
  expect(archive.body.operation_id).toMatch(/^[0-9a-f-]{36}$/i);

  await page.getByRole('button', { name: 'Архив' }).click();
  await expect(page.locator(`[data-template-card="${TEMPLATE_ID}"]`)).toContainText('Архив');
  await page.getByRole('button', { name: 'Восстановить' }).click();
  await page.locator('#tplConfirmOk').click();
  const writes = harness.requests.filter(item => item.path === `/api/admin/task-templates/${TEMPLATE_ID}/status`);
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toMatchObject({ status: 'active', expected_generation: 6 });
});

test('dirty apply требует подтверждение и публикация наследует версию и фото шаблона', async ({ page }) => {
  const template = taskTemplate({ evidence_policy: 'before_and_after_required' });
  const harness = await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [template] }),
    templateStore: { active: [template], archived: [] },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('#ntTitle').fill('Мой несохранённый черновик');
  await page.locator('#ntTemplate').selectOption(TEMPLATE_ID);
  await expect(page.locator('#templateConfirmSheet')).toBeVisible();
  await page.locator('#tplConfirmCancel').click();
  await expect(page.locator('#ntTitle')).toHaveValue('Мой несохранённый черновик');
  await expect(page.locator('#ntTemplate')).toHaveValue('');

  await page.locator('#ntTemplate').selectOption(TEMPLATE_ID);
  await page.locator('#tplConfirmOk').click();
  await expect(page.locator('#ntTitle')).toHaveValue('Поправить парковку байков');
  await expect(page.locator('#ntPhotoPreview')).toHaveAttribute('src', template.photo_url);
  await expect(page.locator('#ntPhotoText')).toContainText('Фото из шаблона');
  await page.evaluate(() => setWizardStep(2, false));
  await page.locator('#ntCity').fill('Краснодар');
  await page.locator('#ntAddr').fill('ул. Красная, 1');
  await page.evaluate(() => setWizardStep(3, false));
  await page.locator('#ntCreate').click();
  await expect(page.locator('#taskPreviewBody img')).toHaveAttribute('src', template.photo_url);
  await page.locator('#ntPublish').click();
  await expect.poll(() => harness.requests.some(item => item.path === '/api/admin/task/create')).toBe(true);
  const request = harness.requests.find(item => item.path === '/api/admin/task/create');
  expect(request.body).toMatchObject({
    template_id: TEMPLATE_ID, template_version_id: TEMPLATE_VERSION_ID, template_photo_action: 'inherit', photo_data: null,
  });
});

test('фото шаблона можно заменить и удалить только для создаваемого задания', async ({ page }) => {
  const template = taskTemplate({ evidence_policy: 'comment_only' });
  await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [template] }),
    templateStore: { active: [template], archived: [] },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('#ntTemplate').selectOption(TEMPLATE_ID);
  await page.evaluate(() => setWizardStep(2, false));
  expect(await page.evaluate(() => collectTaskBody().evidence_policy)).toBe('comment_only');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
  await page.locator('#ntPhoto').setInputFiles({ name: 'parking.png', mimeType: 'image/png', buffer: png });
  await expect(page.locator('#ntPhotoText')).toContainText('Фото готово');
  expect(await page.evaluate(() => collectTaskBody().template_photo_action)).toBe('replace');
  expect(await page.evaluate(() => collectTaskBody().photo_data.startsWith('data:image/jpeg'))).toBe(true);
  await page.locator('#ntPhotoClear').click();
  const body = await page.evaluate(() => collectTaskBody());
  expect(body.template_photo_action).toBe('remove');
  expect(body.photo_data).toBeNull();
});

test('применённый шаблон блокирует authoritative поля и отсоединяется без потери текста', async ({ page }) => {
  const template = taskTemplate();
  await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [template] }),
    templateStore: { active: [template], archived: [] },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('#ntTemplate').selectOption(TEMPLATE_ID);

  for (const id of ['ntType', 'ntTitle', 'ntDetails', 'ntReward', 'ntMode', 'ntEvidence', 'ntMaxParticipants', 'ntBudgetCap']) {
    await expect(page.locator(`#${id}`)).toBeDisabled();
    await expect(page.locator(`#${id}`)).toHaveAttribute('aria-describedby', /ntTemplateLockHint/);
  }
  await expect(page.locator('#ntTemplateLockHint')).toContainText('выбери «Без шаблона»');
  for (const id of ['ntCity', 'ntAddr', 'ntSlotStart', 'ntSlotEnd', 'ntAssigneeSearch', 'ntPhoto']) {
    await expect(page.locator(`#${id}`)).toBeEnabled();
  }

  await page.locator('#ntTemplate').selectOption('');
  await expect(page.locator('#ntTemplateLockHint')).toBeHidden();
  await expect(page.locator('#ntTitle')).toBeEnabled();
  await expect(page.locator('#ntTitle')).toHaveValue(template.task_title);
  await expect(page.locator('#ntPhotoPreview')).toBeHidden();
});

test('поиск сортирует библиотеку по названию и действия называют шаблон для screen reader', async ({ page }) => {
  const first = taskTemplate({
    id: '11111111-1111-4111-8111-111111111112',
    version_id: '71111111-1111-4111-8111-111111111112',
    title: 'Яма у вокзала', task_title: 'Проверить яму',
  });
  const second = taskTemplate({
    id: '11111111-1111-4111-8111-111111111113',
    version_id: '71111111-1111-4111-8111-111111111113',
    title: 'Аварийная парковка', task_title: 'Поправить парковку',
  });
  await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [first, second] }),
    templateStore: { active: [first, second], archived: [] },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('[data-taskmode="taskTemplateMode"]').click();

  await expect(page.locator('#tplLibrary .template-card .tt')).toHaveText(['Аварийная парковка', 'Яма у вокзала']);
  await expect(page.getByRole('button', { name: 'Применить шаблон «Аварийная парковка»' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Изменить шаблон «Аварийная парковка»' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Копировать шаблон «Аварийная парковка»' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Архивировать шаблон «Аварийная парковка»' })).toBeVisible();
  await page.locator('#tplSearch').fill('яма');
  await expect(page.locator('#tplLibrary .template-card')).toHaveCount(1);
  await expect(page.locator('#tplLibraryStatus')).toHaveText('Показано: 1 из 2');
  await expect(page.locator('#tplLibrary')).toContainText('Яма у вокзала');
});

test('последний выбор шаблона побеждает ответы, пришедшие не по порядку', async ({ page }) => {
  const slow = taskTemplate({ title: 'Медленный шаблон', task_title: 'Старый выбор' });
  const fastId = '11111111-1111-4111-8111-111111111114';
  const fast = taskTemplate({
    id: fastId, version_id: '71111111-1111-4111-8111-111111111114',
    title: 'Быстрый шаблон', task_title: 'Последний выбор',
  });
  await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [slow, fast] }),
    templateStore: { active: [slow, fast], archived: [] },
    templateDetailDelays: { [TEMPLATE_ID]: 180, [fastId]: 5 },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('#ntTemplate').selectOption(TEMPLATE_ID);
  await page.locator('#ntTemplate').selectOption(fastId);
  await expect(page.locator('#ntTitle')).toHaveValue('Последний выбор');
  await page.waitForTimeout(220);
  await expect(page.locator('#ntTemplate')).toHaveValue(fastId);
  await expect(page.locator('#ntTitle')).toHaveValue('Последний выбор');
});

test('stale версия сохраняет локальные поля и безопасно применяет обновлённый шаблон', async ({ page }) => {
  const template = taskTemplate({ evidence_policy: 'comment_only' });
  const harness = await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [template] }),
    templateStore: { active: [template], archived: [] },
    taskCreateResponses: [
      { status: 409, body: { error: 'template_version_stale', current_version_id: 'new-version' } },
      { status: 200, body: { ok: true, announcement_status: 'not_requested' } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('#ntTemplate').selectOption(TEMPLATE_ID);
  await page.evaluate(() => setWizardStep(2, false));
  await page.locator('#ntCity').fill('Краснодар');
  await page.locator('#ntAddr').fill('ул. Сохранённая, 7');
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
  await page.locator('#ntPhoto').setInputFiles({ name: 'local.png', mimeType: 'image/png', buffer: png });
  Object.assign(harness.templateStore.active[0], {
    version_id: '71111111-1111-4111-8111-111111111199',
    version_number: 4, generation: 6, task_title: 'Обновлённое задание', reward: 95,
  });
  await page.evaluate(() => setWizardStep(3, false));
  await page.locator('#ntCreate').click();
  await page.locator('#ntPublish').click();

  await expect(page.locator('#ntTemplateRecovery')).toBeVisible();
  await expect(page.locator('#ntTemplateRecoveryText')).toContainText('Локальные место, сроки, исполнитель и фото сохранены');
  await expect(page.locator('#ntCity')).toHaveValue('Краснодар');
  await expect(page.locator('#ntAddr')).toHaveValue('ул. Сохранённая, 7');
  await page.locator('#ntTemplateReapply').click();
  await expect(page.locator('#ntTitle')).toHaveValue('Обновлённое задание');
  await expect(page.locator('#ntReward')).toHaveValue('95');
  await expect(page.locator('#ntPhotoText')).toContainText('Сохранено после обновления');
  await expect(page.locator('#ntTemplateRecovery')).toBeHidden();
  await page.evaluate(() => setWizardStep(3, false));
  await page.locator('#ntCreate').click();
  await expect(page.locator('#taskPreviewBody')).toContainText('Обновлённое задание');
  await expect(page.locator('#taskPreviewBody')).toContainText('+95⚡');
  await page.locator('#ntPublish').click();
  await expect.poll(() => harness.requests.filter(item => item.path === '/api/admin/task/create').length).toBe(2);
  const retry = harness.requests.filter(item => item.path === '/api/admin/task/create')[1];
  expect(retry.body).toMatchObject({
    city: 'Краснодар', address: 'ул. Сохранённая, 7',
    template_version_id: '71111111-1111-4111-8111-111111111199',
    title: 'Обновлённое задание', reward: 95, template_photo_action: 'replace',
  });
  expect(retry.body.photo_data).toMatch(/^data:image\/jpeg/);
});

test('создание, новая версия и копия шаблона используют правильные endpoints', async ({ page }) => {
  const template = taskTemplate();
  const harness = await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [template] }),
    templateStore: { active: [template], archived: [] },
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('[data-taskmode="taskTemplateMode"]').click();

  await page.locator('#tplNew').click();
  await page.locator('#tplName').fill('Фото-проверка');
  await page.locator('#tplTaskTitle').fill('Проверить парковку');
  await page.locator('#tplType').selectOption('photo_check');
  await page.locator('#tplEvidence').selectOption('comment_only');
  await page.locator('#tplReward').fill('50');
  await page.locator('#tplEditorSave').click();
  await expect.poll(() => harness.requests.filter(item => item.path === '/api/admin/task-templates' && item.method === 'POST').length).toBe(1);
  const created = harness.requests.find(item => item.path === '/api/admin/task-templates' && item.method === 'POST');
  expect(created.body.operation_id).toMatch(/^[0-9a-f-]{36}$/i);
  expect(created.body.photo_action).toBe('remove');
  expect(created.body.evidence_policy).toBe('comment_only');

  await page.locator(`[data-template-card="${TEMPLATE_ID}"] [data-tplcopy]`).click();
  await expect(page.locator('#tplName')).toHaveValue('Копия — Парковка у ТЦ');
  await page.locator('#tplEditorSave').click();
  await expect.poll(() => harness.requests.filter(item =>
    item.path === '/api/admin/task-templates' && item.method === 'POST'
  ).length).toBe(2);
  const copies = harness.requests.filter(item => item.path === '/api/admin/task-templates' && item.method === 'POST');
  expect(copies[1].body).toMatchObject({
    copied_from_id: TEMPLATE_ID,
    copied_from_version_id: TEMPLATE_VERSION_ID,
    photo_action: 'keep',
  });

  await page.locator(`[data-template-card="${TEMPLATE_ID}"] [data-tpledit]`).click();
  await page.locator('#tplDetails').fill('Новое описание без изменения старых заданий');
  await page.locator('#tplPhotoRemove').click();
  await page.locator('#tplEditorSave').click();
  await expect.poll(() => harness.requests.some(item => item.path === `/api/admin/task-templates/${TEMPLATE_ID}/versions`)).toBe(true);
  const version = harness.requests.find(item => item.path === `/api/admin/task-templates/${TEMPLATE_ID}/versions`);
  expect(version.body).toMatchObject({ expected_generation: 5, photo_action: 'remove' });
});

test('библиотека загружает все страницы и сохраняет в селекторе больше 50 шаблонов', async ({ page }) => {
  const templates = Array.from({ length: 65 }, (_, index) => {
    const serial = String(index + 1).padStart(12, '0');
    return taskTemplate({
      id: `11111111-1111-4111-8111-${serial}`,
      version_id: `71111111-1111-4111-8111-${serial}`,
      key: `parking-${index + 1}`,
      title: `Шаблон ${index + 1}`,
      task_title: `Задание ${index + 1}`,
    });
  });
  const harness = await openMiniApp(page, {
    initialState: staffState(['task.template.manage', 'task.create'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: templates }),
    templateStore: { active: templates, archived: [] },
    templatePageSize: 50,
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await expect(page.locator('#ntTemplate option')).toHaveCount(66);
  await page.locator('[data-taskmode="taskTemplateMode"]').click();
  await expect(page.locator('#tplLibrary .template-card')).toHaveCount(65);
  await expect(page.locator('#ntTemplate option')).toHaveCount(66);
  await expect(page.locator('#tplLibraryStatus')).toHaveText('Найдено: 65');
  const reads = harness.requests.filter(item =>
    item.path === '/api/admin/task-templates' && item.method === 'GET'
  );
  expect(reads).toHaveLength(2);
  expect(reads[1].query).toContain('after_id=11111111-1111-4111-8111-000000000050');
});

test('неоднозначное сохранение шаблона повторяется с тем же operation_id', async ({ page }) => {
  const harness = await openMiniApp(page, {
    initialState: staffState(['task.template.manage'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview(),
    templateStore: { active: [], archived: [] },
    templateWriteResponses: [
      { status: 503, body: { message: 'Ответ не подтверждён' } },
      { status: 200, body: { ok: true } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('[data-taskmode="taskTemplateMode"]').click();
  await page.locator('#tplNew').click();
  await page.locator('#tplName').fill('Ночной осмотр');
  await page.locator('#tplTaskTitle').fill('Осмотреть парковку ночью');
  await page.locator('#tplReward').fill('60');
  await page.locator('#tplEditorSave').click();
  await expect(page.locator('#templateEditorSheet')).toBeVisible();
  await expect(page.locator('#tplEditorError')).toContainText('Ответ не подтверждён');
  await page.locator('#tplEditorSave').click();
  await expect.poll(() => harness.requests.filter(item => item.path === '/api/admin/task-templates' && item.method === 'POST').length).toBe(2);
  const writes = harness.requests.filter(item => item.path === '/api/admin/task-templates' && item.method === 'POST');
  expect(writes[1].body.operation_id).toBe(writes[0].body.operation_id);
});

test('409 при редактировании сохраняет форму как копию, а mobile библиотека не создаёт горизонтальный скролл', async ({ page }) => {
  const template = taskTemplate();
  await page.setViewportSize({ width: 390, height: 844 });
  const harness = await openMiniApp(page, {
    initialState: staffState(['task.template.manage'], { task_types: TEMPLATE_TASK_TYPES }),
    adminOverview: emptyAdminOverview({ task_templates: [template] }),
    templateStore: { active: [template], archived: [] },
    templateWriteResponses: [
      { status: 409, body: { message: 'generation conflict' } },
      { status: 200, body: { ok: true } },
    ],
  });
  await page.locator('#nav [data-tab="tab-admin"]').click();
  await page.locator('[data-asub="adSubCreate"]').click();
  await page.locator('[data-taskmode="taskTemplateMode"]').click();
  await page.locator(`[data-template-card="${TEMPLATE_ID}"] [data-tpledit]`).click();
  await page.locator('#tplDetails').fill('Изменение, которое нельзя потерять');
  await page.locator('#tplEditorSave').click();
  await expect(page.locator('#templateEditorSheet')).toBeVisible();
  await expect(page.locator('#tplDetails')).toHaveValue('Изменение, которое нельзя потерять');
  await expect(page.locator('#tplEditorError')).toContainText('Введённые данные сохранены');
  const request = harness.requests.find(item => item.path === `/api/admin/task-templates/${TEMPLATE_ID}/versions`);
  expect(request.body.expected_generation).toBe(5);
  await expect(page.locator('#tplEditorCopy')).toBeVisible();
  await page.locator('#tplEditorCopy').click();
  await expect(page.locator('#tplEditorTitle')).toHaveText('Создать копию');
  await expect(page.locator('#tplDetails')).toHaveValue('Изменение, которое нельзя потерять');
  await expect(page.locator('#tplName')).toHaveValue('Копия — Парковка у ТЦ');
  await page.locator('#tplEditorSave').click();
  await expect(page.locator('#templateEditorSheet')).toBeHidden();
  const copy = harness.requests.filter(item => item.path === '/api/admin/task-templates' && item.method === 'POST').at(-1);
  expect(copy.body).toMatchObject({
    copied_from_id: TEMPLATE_ID,
    copied_from_version_id: TEMPLATE_VERSION_ID,
    details: 'Изменение, которое нельзя потерять',
  });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const columns = await page.locator(`[data-template-card="${TEMPLATE_ID}"] .acts`).evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  expect(columns).toBe(2);
});
