import { _t } from '@web/core/l10n/translation';
import { rpc } from '@web/core/network/rpc';
import { patch } from '@web/core/utils/patch';

import { PaymentForm } from '@payment/interactions/payment_form';
import {
    browserInfo,
    collectDeviceData,
    mountMicroform,
    openChallenge,
} from '@mdv_payment_cybersource/js/cybersource_microform';

const PROVIDER_CODE = 'mdv_cybersource';

patch(PaymentForm.prototype, {

    // #=== DOM MANIPULATION ===#

    /**
     * Mount the Microform card fields of CyberSource.
     *
     * @override method from @payment/interactions/payment_form
     */
    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        this._mdvCsDestroy();
        if (providerCode !== PROVIDER_CODE) {
            await super._prepareInlineForm(...arguments);
            return;
        }
        if (flow === 'token') {
            return; // Saved cards are charged server-side.
        }
        this._setPaymentFlow('direct');
        const radio = document.querySelector('input[name="o_payment_radio"]:checked');
        const container = this._getInlineForm(radio)?.querySelector('[name="o_mdv_cs_container"]');
        if (!container) {
            return;
        }
        this.mdvCsContainer = container;
        this._mdvCsShowError('');
        try {
            const context = await this.waitFor(rpc('/payment/mdv_cybersource/capture_context', {
                provider_id: providerId,
            }));
            this.mdvCsMicroform = await this.waitFor(mountMicroform(context, container));
        } catch (error) {
            console.warn('CyberSource card form not available:', error);
            this.mdvCsMicroform = null;
            this._mdvCsShowError(
                error.data?.message || error.message || _t("The card form could not be loaded.")
            );
        }
    },

    // #=== PAYMENT FLOW ===#

    /**
     * Create the transient token before the transaction, so a typo in the card number does not
     * leave a failed transaction behind.
     *
     * @override method from @payment/interactions/payment_form
     */
    async _initiatePaymentFlow(providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== PROVIDER_CODE || flow !== 'direct') {
            await super._initiatePaymentFlow(...arguments);
            return;
        }
        if (!this.mdvCsMicroform) {
            this._displayErrorDialog(
                _t("Payment processing failed"), _t("The card form could not be loaded.")
            );
            this._enableButton();
            return;
        }
        const month = this.mdvCsContainer.querySelector('[name="o_mdv_cs_exp_month"]')?.value;
        const year = this.mdvCsContainer.querySelector('[name="o_mdv_cs_exp_year"]')?.value;
        if (!month || !year) {
            this._mdvCsShowError(_t("Please select the expiry date of the card."));
            this._enableButton();
            return;
        }
        try {
            this.mdvCsToken = await this.waitFor(this.mdvCsMicroform.createToken(month, year));
        } catch (error) {
            this._mdvCsShowError(error.message);
            this._enableButton();
            return;
        }
        this._mdvCsShowError('');
        await super._initiatePaymentFlow(...arguments);
    },

    /**
     * Send the transient token to Odoo, then run 3-D Secure when CyberSource asks for it.
     *
     * @override method from @payment/interactions/payment_form
     */
    async _processDirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        if (providerCode !== PROVIDER_CODE) {
            await super._processDirectFlow(...arguments);
            return;
        }
        const token = this.mdvCsToken;
        this.mdvCsToken = null;
        if (!processingValues.mdv_cs_tx_id || !token) {
            this._displayErrorDialog(
                _t("Payment processing failed"), processingValues.state_message || ''
            );
            this._enableButton();
            return;
        }
        const txParams = {
            tx_id: processingValues.mdv_cs_tx_id,
            access_token: processingValues.mdv_cs_access_token,
        };
        try {
            let result = await this.waitFor(rpc('/payment/mdv_cybersource/pay', {
                ...txParams, transient_token: token, browser: browserInfo(),
            }));
            if (result.action === 'collect') {
                await this.waitFor(collectDeviceData(result.url, result.jwt));
                result = await this.waitFor(rpc('/payment/mdv_cybersource/authenticate', {
                    ...txParams, browser: browserInfo(),
                }));
            }
            if (result.action === 'challenge') {
                openChallenge(result.url, result.jwt, result.md);
                return;
            }
            window.location = result.redirect_url || '/payment/status';
        } catch (error) {
            this._displayErrorDialog(
                _t("Payment processing failed"), error.data?.message || error.message || ''
            );
            this._enableButton();
        }
    },

    // #=== CYBERSOURCE HELPERS ===#

    /**
     * @private
     * @param {string} message
     * @return {void}
     */
    _mdvCsShowError(message) {
        const alert = this.mdvCsContainer?.querySelector('[name="o_mdv_cs_error"]');
        if (!alert) {
            return;
        }
        alert.textContent = message || '';
        alert.classList.toggle('d-none', !message);
    },

    /**
     * Remove the Microform fields of a previously selected payment method.
     *
     * @private
     * @return {void}
     */
    _mdvCsDestroy() {
        if (this.mdvCsMicroform) {
            this.mdvCsMicroform.destroy();
            this.mdvCsMicroform = null;
        }
        this.mdvCsToken = null;
    },
});
