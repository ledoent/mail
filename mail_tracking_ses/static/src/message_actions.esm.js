import {_t} from "@web/core/l10n/translation";
import {registerMessageAction} from "@mail/core/common/message_actions";

registerMessageAction("ses-delivery", {
    condition: ({message, thread}) =>
        message.id > 0 && thread?.model !== "discuss.channel",
    icon: "fa fa-envelope-o",
    name: _t("Email delivery"),
    sequence: 95,
    onSelected: async ({message, owner}) => {
        const action = await owner.env.services.orm.call(
            "mail.message",
            "action_ses_delivery",
            [[message.id]]
        );
        await owner.env.services.action.doAction(action);
    },
});
