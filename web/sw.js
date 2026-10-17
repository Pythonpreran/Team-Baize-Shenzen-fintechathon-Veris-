"use strict";
/* VERIS service worker: the installable app's SHELL, and nothing else.
 *
 * WHAT IT MAY STORE. The fixed list below: the stylesheet, the script, the translation
 * catalogues, the manifest, the icons and a static offline page. Every
 * one is a public file under /static/ that hosted mode serves without sign-in, so a copy
 * on the device discloses nothing about anybody.
 *
 * WHAT IT NEVER STORES, and why each rule is a rule rather than a default:
 *   - any /api/ response. Portfolio, claim and evidence answers are tenant data; they
 *     are passed straight through to the network and never touched;
 *   - any navigation. In hosted mode /app is an authenticated document, so a page is
 *     fetched from the network every time and the offline page is only a fallback;
 *   - any non-GET request, any cross-origin request, any URL with a query string, and
 *     any response that is not a plain same-origin 200.
 *
 * Network first for the shell, so a deployed fix reaches the browser immediately; the
 * cache answers only when the network cannot. Signing out posts `veris:clear-caches`
 * and app.js also deletes the caches itself, so a shared device keeps nothing after a
 * session ends.
 */
const CACHE_PREFIX = "veris-shell-";
// v2 (T28): the Simplified Chinese catalogue joined the shell, so the offline page can
// speak the reader's chosen language. A new version name makes installed workers refetch.
const SHELL_CACHE = `${CACHE_PREFIX}v2`;
const OFFLINE_PAGE = "/static/offline.html";
const SHELL_ASSETS = Object.freeze([
  "/static/styles.css",
  "/static/app.js",
  "/static/offline.js",
  "/static/i18n/en.json",
  "/static/i18n/zh-CN.json",
  "/static/manifest.webmanifest",
  "/static/icons/veris.svg",
  "/static/icons/veris-192.png",
  "/static/icons/veris-512.png",
  OFFLINE_PAGE,
]);
const SHELL_PATHS = new Set(SHELL_ASSETS);

function cacheable(request, url) {
  return request.method === "GET"
    && url.origin === self.location.origin
    && !url.pathname.startsWith("/api/")
    && request.mode !== "navigate"
    && !url.search
    && SHELL_PATHS.has(url.pathname);
}

async function clearShellCaches() {
  const keys = await caches.keys();
  await Promise.all(keys.filter(key => key.startsWith(CACHE_PREFIX)).map(key => caches.delete(key)));
}

async function networkFirst(request, url) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok && response.status === 200 && response.type === "basic" && !response.redirected) {
      await cache.put(url.pathname, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await cache.match(url.pathname);
    if (cached) return cached;
    throw error;
  }
}

async function offlineNavigation(request) {
  try {
    return await fetch(request);
  } catch (error) {
    const cached = await caches.match(OFFLINE_PAGE);
    if (cached) return cached;
    throw error;
  }
}

self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL_CACHE)
    .then(cache => cache.addAll(SHELL_ASSETS))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys
      .filter(key => key.startsWith(CACHE_PREFIX) && key !== SHELL_CACHE)
      .map(key => caches.delete(key))))
    .then(() => self.clients.claim()));
});

self.addEventListener("message", event => {
  if (event.data && event.data.type === "veris:clear-caches") {
    event.waitUntil(clearShellCaches());
  }
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate") {
    event.respondWith(offlineNavigation(request));
    return;
  }
  if (!cacheable(request, url)) return;
  event.respondWith(networkFirst(request, url));
});
