/* ==========================================================================
   Présence App - Service Worker (PWA)
   Gestion du cache, des assets statiques et de l'accès hors-ligne
   ========================================================================== */

const CACHE_NAME = 'presence-app-v1.0';
const STATIC_ASSETS = [
    '/',
    '/static/style.css',
    '/static/manifest.json',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/static/icons/apple-touch-icon.png',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css',
    'https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=Nunito:wght@200;300;400;600;700;800;900&display=swap',
    'https://cdn.jsdelivr.net/npm/startbootstrap-sb-admin-2@4.1.4/css/sb-admin-2.min.css',
    'https://cdnjs.cloudflare.com/ajax/libs/jquery/3.6.0/jquery.min.js',
    'https://cdn.jsdelivr.net/npm/bootstrap@4.6.0/dist/js/bootstrap.bundle.min.js',
    'https://cdn.jsdelivr.net/npm/startbootstrap-sb-admin-2@4.1.4/js/sb-admin-2.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/2.9.4/Chart.min.js'
];

// Installation : Mise en cache des assets statiques initiaux
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            // Utiliser addAll avec tolérance aux échecs pour les CDNs
            return Promise.allSettled(
                STATIC_ASSETS.map(url => cache.add(url).catch(err => {
                    console.warn('[PWA SW] Pre-cache échoué pour:', url, err);
                }))
            );
        }).then(() => {
            return self.skipWaiting();
        })
    );
});

// Activation : Nettoyage des anciens caches
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        console.log('[PWA SW] Nettoyage ancien cache:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        }).then(() => {
            return self.clients.claim();
        })
    );
});

// Interception des requêtes
self.addEventListener('fetch', (event) => {
    const request = event.request;
    const url = new URL(request.url);

    // Ignorer les requêtes non-GET et les requêtes de téléversement/export
    if (request.method !== 'GET') {
        return;
    }

    // 1. Assets Statiques (CSS, JS, Images, Polices) -> Stale-While-Revalidate
    if (
        url.pathname.startsWith('/static/') ||
        url.hostname.includes('fonts.googleapis.com') ||
        url.hostname.includes('fonts.gstatic.com') ||
        url.hostname.includes('cdnjs.cloudflare.com') ||
        url.hostname.includes('cdn.jsdelivr.net')
    ) {
        event.respondWith(
            caches.open(CACHE_NAME).then((cache) => {
                return cache.match(request).then((cachedResponse) => {
                    const fetchPromise = fetch(request).then((networkResponse) => {
                        if (networkResponse && networkResponse.status === 200) {
                            cache.put(request, networkResponse.clone());
                        }
                        return networkResponse;
                    }).catch(() => cachedResponse);

                    return cachedResponse || fetchPromise;
                });
            })
        );
        return;
    }

    // 2. Navigation HTML & Données d'appel -> Network First avec fallback
    // Crucial : Les données d'appel et notes doivent toujours venir du serveur en direct !
    if (request.mode === 'navigate' || request.headers.get('accept')?.includes('text/html')) {
        event.respondWith(
            fetch(request).catch(() => {
                return caches.match(request).then((cachedResponse) => {
                    if (cachedResponse) {
                        return cachedResponse;
                    }
                    // Page ou message de secours en cas de déconnexion totale
                    return new Response(
                        `<!DOCTYPE html>
                        <html lang="fr">
                        <head>
                            <meta charset="utf-8">
                            <meta name="viewport" content="width=device-width, initial-scale=1">
                            <title>Mode Hors-Ligne - Présence App</title>
                            <link href="https://cdn.jsdelivr.net/npm/startbootstrap-sb-admin-2@4.1.4/css/sb-admin-2.min.css" rel="stylesheet">
                        </head>
                        <body class="bg-light d-flex align-items-center justify-content-center" style="min-height: 100vh;">
                            <div class="text-center p-4 bg-white rounded shadow-sm" style="max-width: 480px;">
                                <div class="mb-3 text-warning">
                                    <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <line x1="1" y1="1" x2="23" y2="23"></line>
                                        <path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"></path>
                                        <path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"></path>
                                        <path d="M10.71 5.05A16 16 0 0 1 22.58 9"></path>
                                        <path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"></path>
                                        <path d="M8.53 16.11a6 6 0 0 1 6.95 0"></path>
                                        <line x1="12" y1="20" x2="12.01" y2="20"></line>
                                    </svg>
                                </div>
                                <h4 class="font-weight-bold text-dark">Connexion Interrompue</h4>
                                <p class="text-muted small">Vous êtes actuellement en mode hors-ligne. Veuillez vérifier votre connexion réseau ou Wi-Fi pour accéder aux données en direct.</p>
                                <button class="btn btn-primary btn-sm px-4" onclick="window.location.reload()">
                                    Réessayer
                                </button>
                            </div>
                        </body>
                        </html>`,
                        { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
                    );
                });
            })
        );
        return;
    }
});
