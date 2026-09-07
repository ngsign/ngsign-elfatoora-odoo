/** @odoo-module **/

import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { BlockUI } from "@web/core/ui/block_ui";

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

        if (!isValidation) {
            notification.add(_t("Process completed successfully."), { type: "success" });
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

        // Always reload the view to show updated status
        return { type: "ir.actions.client", tag: "reload" };

    } catch (error) {
        console.error("NGSign Error:", error);
        // Re-throw to let Odoo handle the error dialog
        throw error;
    } finally {
        unblock();
    }
}

registry.category("actions").add("ngsign_einvoice_odoo.action_sign_ngsign_js", actionSignNGSignJs);
