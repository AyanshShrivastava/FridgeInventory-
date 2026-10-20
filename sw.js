// Service Worker for FridgeInventory Web Push Notifications
const CACHE_NAME = 'fridge-inventory-v2.2';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

// Push event listener: receives notification payload from web-push
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

// Notification click event: focus existing tab or open new window
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
