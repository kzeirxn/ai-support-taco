# 🧠 Taco Licensing – AI Knowledgebase

## 📌 Metadata

* **Version:** 1.0
* **Last Updated:** 2026-05-02
* **Description:** Structured support knowledgebase for Taco Licensing platform

---

# 🔐 Authentication & Accounts

## How does login work?

Users authenticate using a token stored in `localStorage` under `taco_token`.
If the token becomes invalid or expires, the user is automatically logged out.

## What happens when an account is suspended?

* Suspended users cannot log in
* A modal explains the reason and who issued the suspension
* User must contact support

## Two-Factor Authentication (2FA)

### How to enable

* Scan QR code using an authenticator app (Google Authenticator, Authy)
* Save backup codes securely

### Common issues

* Invalid code → check device time sync
* Lost access → use backup codes
* Disabling requires account password

---

# 🧩 Workspaces

## What is a workspace?

A workspace is the main container for:

* Products
* Licenses
* Members
* Webhooks
* Audit logs

## Workspace actions

* Create, update, delete
* Suspend / unsuspend
* Manage members and roles

## Workspace suspension

* Features are restricted
* Users receive system notifications
* Only staff can restore access

---

# 📦 Products

## Managing products

* Create, update, delete
* Link licenses
* Suspend or restore

## Product status

* **Active** → usable
* **Suspended** → disabled

---

# 🔑 Licenses

## License management

* Create individual licenses
* Bulk create licenses
* Activate / deactivate
* Delete or transfer

## Bulk operations

* Bulk create
* Bulk toggle
* Bulk delete

---

# 👥 Members & Roles

## Member management

* Add/remove members
* Assign roles

## Permissions

Roles determine:

* Access level
* Allowed actions

## Common issue

**Permission denied** → insufficient role permissions

---

# 🔔 Notifications

## How notifications work

Tracks:

* License events
* Product updates
* Member actions
* Workspace changes

## Notes

* Grouped by workspace
* Shows recent activity

---

# 📊 Status Center

## What it monitors

* API health
* Bot/Discord integration
* Workspace health
* Product health

## Status meanings

* 🟢 Online → healthy
* 🟡 Degraded → partial issues
* 🔴 Offline → unavailable

---

# 🔌 API & Errors

## Base URL

```
https://app.tacolicensing.org
```

## Health check

```
GET /api/v1/health
```

## Common errors

* `401` → token expired
* `STAFF_ACTION_APPROVAL_REQUIRED` → admin approval needed
* Generic errors → shown via toast

---

# 🧾 Audit Logs

## Tracks

* Workspace changes
* Product updates
* License actions
* Member activity

## Purpose

* Debugging
* Activity tracking
* Security auditing

---

# 💬 Messaging

## System messages

Triggered by:

* Workspace suspension
* Product suspension

## Access

Available via `/messages`

---

# ⚙️ Settings

## Theme

* Light / Dark / Auto
* Auto follows system preference

---

# 🔍 Command Palette

## Shortcut

```
CTRL + K
```

## Features

* Quick navigation
* Open workspaces
* Access key pages

---

# 🔐 Sessions & Security

## Session management

* View active sessions
* Revoke individual sessions
* Revoke all other sessions

---

# 🔗 Webhooks & Integrations

## Supported events

* Product updates
* License events
* Workspace actions

## Actions

* Create webhook
* Delete webhook
* Toggle webhook

---

# ❗ Troubleshooting

## API unreachable

Backend may be down or network issue → check Status Center

## Cannot create a license

* Check permissions
* Ensure product is not suspended

## Product not visible

* Check correct workspace
* Ensure product is not deleted/suspended

## Permission denied

User role does not allow the action

## Session expired

User must log in again

---

# 🏁 Summary

Taco Licensing is a:

* Multi-workspace SaaS platform
* Product and license management system
* Role-based access control system
* Audit and activity tracking system

---
