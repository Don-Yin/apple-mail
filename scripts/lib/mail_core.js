/**
 * apple mail JXA core library
 *
 * batch-optimized Mail.app automation via JavaScript for Automation.
 * injected into all JXA scripts to provide consistent account resolution,
 * case-insensitive mailbox lookup, and batch property fetching.
 */

const Mail = Application("Mail");

const MailCore = {
    getAccountByEmail(email) {
        const accounts = Mail.accounts();
        if (!email) {
            if (accounts.length === 0) throw new Error("no mail accounts configured");
            return accounts[0];
        }
        const target = email.toLowerCase();
        const allEmails = Mail.accounts.emailAddresses();
        for (let i = 0; i < accounts.length; i++) {
            for (let j = 0; j < allEmails[i].length; j++) {
                if (allEmails[i][j].toLowerCase() === target) return accounts[i];
            }
        }
        throw new Error("no account found for email: " + email);
    },

    getAccountByName(name) {
        if (!name) {
            const accounts = Mail.accounts();
            if (accounts.length === 0) throw new Error("no mail accounts configured");
            return accounts[0];
        }
        return Mail.accounts.byName(name);
    },

    getMailbox(account, name) {
        const target = name.toLowerCase();
        const mboxes = account.mailboxes();
        const names = account.mailboxes.name();
        for (let i = 0; i < names.length; i++) {
            if (names[i].toLowerCase() === target) return mboxes[i];
        }
        throw new Error("mailbox not found: " + name);
    },

    batchFetch(msgs, props, limit) {
        const result = {};
        for (const prop of props) {
            const all = msgs[prop]();
            result[prop] = (limit && limit > 0) ? all.slice(0, limit) : all;
        }
        return result;
    },

    formatDate(date) {
        if (!date || !(date instanceof Date)) return null;
        return date.toISOString();
    },

    today() {
        const d = new Date();
        d.setHours(0, 0, 0, 0);
        return d;
    },

    daysAgo(days) {
        const d = new Date();
        d.setDate(d.getDate() - days);
        d.setHours(0, 0, 0, 0);
        return d;
    },

    listAccounts() {
        const names = Mail.accounts.name();
        const users = Mail.accounts.userName();
        const emails = Mail.accounts.emailAddresses();
        const results = [];
        for (let i = 0; i < names.length; i++) {
            results.push({
                name: names[i],
                user: users[i],
                emails: emails[i].join(",")
            });
        }
        return results;
    },

    listMailboxes(account) {
        const names = account.mailboxes.name();
        const results = [];
        for (let i = 0; i < names.length; i++) {
            results.push({ name: names[i] });
        }
        return results;
    },

    listMailboxesWithCounts(account) {
        const mboxes = account.mailboxes();
        const names = account.mailboxes.name();
        const results = [];
        for (let i = 0; i < mboxes.length; i++) {
            let count = 0;
            try { count = mboxes[i].messages.id().length; } catch(e) {}
            results.push({
                folder_name: names[i],
                email_count: count
            });
        }
        return results;
    },

    getEmailsByIds(mailbox, targetIds) {
        const allIds = mailbox.messages.id();
        const results = [];
        for (const tid of targetIds) {
            const idx = allIds.indexOf(tid);
            if (idx !== -1) results.push(mailbox.messages[idx]);
        }
        return results;
    },

    findMessageById(account, targetId) {
        const mboxes = account.mailboxes();
        const names = account.mailboxes.name();
        const inboxFirst = [];
        const rest = [];
        for (let i = 0; i < names.length; i++) {
            if (names[i].toLowerCase() === "inbox") inboxFirst.push(mboxes[i]);
            else rest.push(mboxes[i]);
        }
        const ordered = inboxFirst.concat(rest);
        for (const mb of ordered) {
            const ids = mb.messages.id();
            const idx = ids.indexOf(targetId);
            if (idx !== -1) return mb.messages[idx];
        }
        return null;
    },

    findMessageAcrossAccounts(targetId) {
        const accounts = Mail.accounts();
        for (const acc of accounts) {
            const msg = MailCore.findMessageById(acc, targetId);
            if (msg) return msg;
        }
        return null;
    },

    findMessageByMessageId(messageId) {
        const accounts = Mail.accounts();
        for (const acc of accounts) {
            const mboxes = acc.mailboxes();
            for (const mb of mboxes) {
                const msgs = mb.messages.whose({messageId: messageId})();
                if (msgs.length > 0) return msgs[0];
            }
        }
        return null;
    },

    resolveByMessageId(messageId, hintAccount, hintMailbox) {
        var accounts = Mail.accounts();
        var accEmails = Mail.accounts.emailAddresses();
        // tier 1: scoped to hinted mailbox
        if (hintAccount && hintMailbox) {
            try {
                var acc = this.getAccountByEmail(hintAccount);
                var mbox = this.getMailbox(acc, hintMailbox);
                var results = mbox.messages.whose({messageId: messageId})();
                if (results.length > 0) return {msg: results[0], account: hintAccount, mailbox: hintMailbox};
            } catch(e) {}
        }
        // tier 2: scan each account's inbox
        for (var i = 0; i < accounts.length; i++) {
            var email = accEmails[i].length > 0 ? accEmails[i][0] : accounts[i].name();
            try {
                var ib = accounts[i].mailboxes.whose({name: "Inbox"})()[0];
                if (!ib) continue;
                var results = ib.messages.whose({messageId: messageId})();
                if (results.length > 0) return {msg: results[0], account: email, mailbox: ib.name()};
            } catch(e) {}
        }
        // tier 3: scan all mailboxes
        for (var i = 0; i < accounts.length; i++) {
            var email = accEmails[i].length > 0 ? accEmails[i][0] : accounts[i].name();
            var mboxes = accounts[i].mailboxes();
            for (var j = 0; j < mboxes.length; j++) {
                try {
                    var results = mboxes[j].messages.whose({messageId: messageId})();
                    if (results.length > 0) return {msg: results[0], account: email, mailbox: mboxes[j].name()};
                } catch(e) {}
            }
        }
        return null;
    }
};
