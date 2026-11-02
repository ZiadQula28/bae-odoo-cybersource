import { _t } from '@web/core/l10n/translation';

/**
 * Helpers around CyberSource Microform v2 and Payer Authentication (3-D Secure 2).
 *
 * Microform: the capture context (a JWT made by Odoo with POST /microform/v2/sessions) names the
 * client library and its integrity hash. `new Flex(captureContext).microform()` renders the card
 * number and security code in iframes served by CyberSource; `microform.createToken()` returns a
 * transient token (a JWT, valid 15 minutes) that Odoo sends to the Payments API. The card data
 * never reaches Odoo.
 *
 * Payer Authentication: the device data collection page of Cardinal is posted in a hidden iframe
 * with the access token of the setup; a challenge (step-up) page is posted in a visible iframe.
 */

const ALLOWED_LIBRARY_ORIGINS = ['https://testflex.cybersource.com', 'https://flex.cybersource.com'];
const COLLECT_TIMEOUT = 10000; // CyberSource: continue after 10 seconds without an answer.

const libraries = new Map();

/**
 * Load the Microform library named by the capture context, with its integrity hash.
 *
 * @param {string} url
 * @param {string} integrity
 * @return {Promise<void>}
 */
export function loadMicroformLibrary(url, integrity) {
    if (!ALLOWED_LIBRARY_ORIGINS.some((origin) => url.startsWith(origin + '/'))) {
        return Promise.reject(new Error(_t("Unexpected card form library.")));
    }
    if (window.Flex && libraries.has(url)) {
        return libraries.get(url);
    }
    const promise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = url;
        if (integrity) {
            script.integrity = integrity;
            script.crossOrigin = 'anonymous';
        }
        script.onload = () => resolve();
        script.onerror = () => {
            libraries.delete(url);
            reject(new Error(_t("The card form could not be loaded.")));
        };
        document.head.appendChild(script);
    });
    libraries.set(url, promise);
    return promise;
}

/**
 * Mount the card number and security code fields.
 *
 * @param {object} context - {capture_context, client_library, client_library_integrity}
 * @param {HTMLElement} container - The `o_mdv_cs_container` element.
 * @return {Promise<{createToken: function(string, string): Promise<string>, destroy: function}>}
 */
export async function mountMicroform(context, container) {
    await loadMicroformLibrary(context.client_library, context.client_library_integrity);
    if (typeof window.Flex !== 'function') {
        throw new Error(_t("The card form could not be loaded."));
    }
    const styles = {
        input: { 'font-size': '15px', 'font-family': 'inherit', color: '#212529' },
        ':focus': { color: '#212529' },
        valid: { color: '#198754' },
        invalid: { color: '#dc3545' },
        '::placeholder': { color: '#6c757d' },
    };
    const flex = new window.Flex(context.capture_context);
    const microform = flex.microform({ styles });
    const numberEl = container.querySelector('[name="o_mdv_cs_number"]');
    const cvvEl = container.querySelector('[name="o_mdv_cs_cvv"]');
    const brandEl = container.querySelector('[name="o_mdv_cs_brand"]');
    const number = microform.createField('number', { placeholder: '0000 0000 0000 0000' });
    const securityCode = microform.createField('securityCode', { placeholder: '***' });
    number.on('change', (data) => {
        const card = (data && data.card && data.card[0]) || null;
        if (brandEl) {
            brandEl.textContent = card ? (card.brandedName || card.name || '') : '';
        }
    });
    number.load(`#${numberEl.id}`);
    securityCode.load(`#${cvvEl.id}`);
    return {
        createToken(month, year) {
            return new Promise((resolve, reject) => {
                microform.createToken({ expirationMonth: month, expirationYear: year }, (err, token) => {
                    if (err) {
                        const details = (err.details || [])
                            .map((d) => d.message || d.location)
                            .filter(Boolean);
                        reject(new Error(
                            details.join(' ') || err.message
                            || _t("Please check the card number, expiry date and security code.")
                        ));
                        return;
                    }
                    resolve(token);
                });
            });
        },
        destroy() {
            for (const field of [number, securityCode]) {
                try {
                    field.unload();
                } catch {
                    // Already removed with the inline form.
                }
            }
        },
    };
}

/**
 * The browser values of 3-D Secure 2 (deviceInformation.httpBrowser*).
 *
 * @return {object}
 */
export function browserInfo() {
    let javaEnabled = false;
    try {
        javaEnabled = Boolean(navigator.javaEnabled && navigator.javaEnabled());
    } catch {
        javaEnabled = false;
    }
    return {
        httpBrowserColorDepth: String(window.screen.colorDepth || 24),
        httpBrowserJavaEnabled: javaEnabled ? 'Y' : 'N',
        httpBrowserJavaScriptEnabled: 'Y',
        httpBrowserLanguage: navigator.language || 'en-US',
        httpBrowserScreenHeight: String(window.screen.height || 0),
        httpBrowserScreenWidth: String(window.screen.width || 0),
        httpBrowserTimeDifference: String(new Date().getTimezoneOffset()),
    };
}

/**
 * Post `fields` to `url` in an iframe.
 *
 * @param {HTMLIFrameElement} iframe
 * @param {string} url
 * @param {object} fields
 * @return {void}
 */
function postToIframe(iframe, url, fields) {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = url;
    form.target = iframe.name;
    form.style.display = 'none';
    for (const [name, value] of Object.entries(fields)) {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        input.value = value;
        form.appendChild(input);
    }
    document.body.appendChild(form);
    form.submit();
    form.remove();
}

/**
 * Run the device data collection of Payer Authentication in a hidden iframe.
 *
 * @param {string} url - deviceDataCollectionUrl
 * @param {string} jwt - accessToken of the setup
 * @return {Promise<void>} resolved when Cardinal answers, or after 10 seconds.
 */
export function collectDeviceData(url, jwt) {
    return new Promise((resolve) => {
        const iframe = document.createElement('iframe');
        iframe.name = `o_mdv_cs_ddc_${Date.now()}`;
        iframe.title = 'Device data collection';
        iframe.style.cssText = 'display:none;width:1px;height:1px;border:0';
        document.body.appendChild(iframe);
        const origin = new URL(url).origin;
        let done = false;
        const finish = () => {
            if (done) {
                return;
            }
            done = true;
            window.removeEventListener('message', onMessage);
            clearTimeout(timer);
            setTimeout(() => iframe.remove(), 0);
            resolve();
        };
        const onMessage = (event) => {
            if (event.origin === origin) {
                finish();
            }
        };
        const timer = setTimeout(finish, COLLECT_TIMEOUT);
        window.addEventListener('message', onMessage);
        postToIframe(iframe, url, { JWT: jwt });
    });
}

/**
 * Show the challenge page of the bank in a modal window. The bank sends the customer back to
 * Odoo inside the window, and that page moves the whole page to the payment status.
 *
 * @param {string} url - stepUpUrl
 * @param {string} jwt - accessToken of the enrollment check
 * @param {string} md - merchant data echoed back to Odoo
 * @return {void}
 */
export function openChallenge(url, jwt, md) {
    const overlay = document.createElement('div');
    overlay.className = 'o_mdv_cs_challenge';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    const box = document.createElement('div');
    box.className = 'o_mdv_cs_challenge_box';
    const title = document.createElement('p');
    title.className = 'o_mdv_cs_challenge_title';
    title.textContent = _t("Verify the payment with your bank");
    const iframe = document.createElement('iframe');
    iframe.name = `o_mdv_cs_stepup_${Date.now()}`;
    iframe.title = _t("3-D Secure verification");
    iframe.className = 'o_mdv_cs_challenge_frame';
    box.appendChild(title);
    box.appendChild(iframe);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    window.addEventListener('message', (event) => {
        if (event.origin === window.location.origin && event.data?.mdvCybersource === 'done') {
            window.location = '/payment/status';
        }
    });
    postToIframe(iframe, url, { JWT: jwt, MD: md });
}
