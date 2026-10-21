// Service Worker for FridgeInventory Web Push & Offline Notifications
const CACHE_NAME = 'fridge-inventory-v2.3';

const OFFLINE_SHELL_ASSETS = [
  '/',
  '/favicon.ico',
];

// Install: Cache essential app shell
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(OFFLINE_SHELL_ASSETS).catch(() => {});
    })
  );
  self.skipWaiting();
});

// Activate: Clean old caches and claim clients immediately
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    }).then(() => self.clients.claim())
  );
});

// Offline Fetch Handler: Network first with Cache fallback for app shell
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  // Let API requests go directly to network
  if (event.request.url.includes('/api/')) return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response && response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match('/')))
  );
});

// Message Event: Allows client to trigger local notifications even when internet is disconnected
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SHOW_OFFLINE_NOTIFICATION') {
    const title = event.data.title || 'Fridge Expiry Alert';
    const options = {
      body: event.data.body || 'An item in your fridge is expiring today!',
      icon: event.data.icon || '/favicon.ico',
      badge: event.data.badge || '/favicon.ico',
      tag: event.data.tag || 'offline-alert',
      renotify: true,
      vibrate: [200, 100, 200],
      data: { url: event.data.url || '/' },
      actions: [
        { action: 'open', title: 'Open Fridge' },
        { action: 'dismiss', title: 'Dismiss' },
      ],
    };
    event.waitUntil(self.registration.showNotification(title, options));
  }
});

// Push Event: Handles incoming background push notifications (even when tab/browser is closed)
self.addEventListener('push', (event) => {
  let data = {
    title: 'Fridge Inventory Alert',
    body: 'An item in your fridge is expiring soon!',
    icon: '/favicon.ico',
    badge: '/favicon.ico',
    url: '/',
    tag: 'fridge-alert',
  };

  if (event.data) {
    try {
      data = { ...data, ...event.data.json() };
    } catch (e) {
      data.body = event.data.text();
    }
  }

  const options = {
    body: data.body,
    icon: data.icon || '/favicon.ico',
    badge: data.badge || '/favicon.ico',
    tag: data.tag || 'fridge-alert',
    renotify: true,
    requireInteraction: data.tag === 'expiry-alert',
    vibrate: [200, 100, 200],
    data: { url: data.url || '/' },
    actions: [
      { action: 'open', title: 'Open Fridge' },
      { action: 'dismiss', title: 'Dismiss' },
    ],
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

// Notification Click Event: Focus existing window or open a new one
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  if (event.action === 'dismiss') return;

  const urlToOpen = event.notification.data?.url || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes(self.registration.scope) && 'focus' in client) {
          return client.focus();
        }
      }
      return clients.openWindow(urlToOpen);
    })
  );
});
