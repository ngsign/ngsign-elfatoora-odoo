/** @odoo-module **/

import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { sprintf } from "@web/core/utils/strings";

const VALIDATION_MODEL = "ngsign.validation.result";

async function actionSignNGSignJs(env, action) {
    const orm = env.services.orm;
    const ui = env.services.ui;
    const notification = env.services.notification;

    const actionService = env.services.action;

    const activeIds = (action.context && action.context.active_ids) || (action.params && action.params.active_ids);

    if (!activeIds || activeIds.length === 0) {
        notification.add(_t("No invoices selected."), { type: "warning" });
        return;
    }

    // ui.block/ui.unblock are counted by Odoo: keep them strictly balanced,
    // otherwise the overlay stays up or the console fills with warnings.
    let blocked = 0;
    const block = (message) => {
        ui.block({ message });
        blocked++;
    };
    const unblock = () => {
        if (blocked > 0) {
            ui.unblock();
            blocked--;
        }
    };

    // This action is launched from the "Sign with NGSign" wizard dialog: close it
    // right away so the user cannot confirm twice while the process runs. From
    // the list view (no dialog) this is a no-op.
    await actionService.doAction({ type: "ir.actions.act_window_close" });

    block(_t("Checking your eInvoice(s)"));

    try {
        const context = Object.assign({}, action.context || {});

        // Step 0: check the data BEFORE rendering any PDF. A batch that has to be
        // corrected then costs nothing and leaves no attachment behind.
        const check = await orm.call("account.move", "action_ngsign_check_before_send",
                                     [activeIds], { context: context });
        if (check && check.res_model === VALIDATION_MODEL) {
            // Release the overlay first: it would sit on top of the dialog.
            unblock();
            await actionService.doAction(check, {
                // The user may have corrected records from the dialog.
                onClose: () => actionService.doAction({ type: "ir.actions.client", tag: "reload" }),
            });
            return;
        }

        unblock();
        block(_t("Preparing your eInvoice(s)"));

        // Step 1: Prepare (Generate PDFs)
        await orm.call("account.move", "action_ngsign_prepare", [activeIds], { context: context });

        // Step 2: Send (update the message and call the API)
        unblock();
        block(_t("Sending eInvoice(s) for signature"));

        const result = await orm.call("account.move", "action_ngsign_send", [activeIds], { context: context });

        // Second line of defence: the backend gate can still return the check
        // wizard (direct call, stale client, data changed since step 0).
        const isValidation = result && result.res_model === VALIDATION_MODEL;
        // The backend may already describe the outcome (e.g. email sent / not sent).
        const backendMessage = result && result.tag === "display_notification";

        if (!isValidation && !backendMessage) {
            const signer = context.ngsign_send_to_user_name;
            let message;
            if (result && result.type === "ir.actions.act_url") {
                message = signer
                    ? sprintf(_t("Transaction created for %s. The signing page opens in a new tab."), signer)
                    : _t("Transaction created. The signing page opens in a new tab.");
            } else {
                message = _t("eInvoice(s) sent to NGSign for signature.");
            }
            notification.add(message, { title: _t("NGSign"), type: "success" });
        }

        // If result contains an action (e.g. act_url for DigiGO, or the data check), execute it
        if (result && result.type) {
            unblock();
            await actionService.doAction(result, isValidation ? {
                onClose: () => actionService.doAction({ type: "ir.actions.client", tag: "reload" }),
            } : {});
        }

        if (isValidation) {
            // Keep the wizard open: reloading would close it.
            return;
        }

        // Refresh the current view to show the new status. A soft reload keeps the
        // confirmation notification on screen, unlike a full "reload".
        return { type: "ir.actions.client", tag: "soft_reload" };

    } catch (error) {
        console.error("NGSign Error:", error);
        // Re-throw to let Odoo handle the error dialog
        throw error;
    } finally {
        unblock();
    }
}

registry.category("actions").add("ngsign_einvoice_odoo.action_sign_ngsign_js", actionSignNGSignJs);
