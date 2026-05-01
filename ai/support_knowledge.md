# Taco Licensing Dashboard Knowledgebase

> Purpose: Accurate support for dashboard, marketplace, and admin features.
> Rule: If unsure, ask one clarifying question and avoid guessing.

# The API is https://app.tacolicensing.org and the frontend url for the dashboard is https://licensing.tacogroup.uk

---

## 1) Feature Map (Routes → Features)

### Public & Auth
- **/login**: Login
- **/register**: Register
- **/auth/success**: OAuth callback

### Marketplace (Public)
- **/marketplace**: Marketplace home
- **/marketplace/workspace/:workspaceId**: Workspace storefront
- **/marketplace/product/:productId**: Public product detail page

### Workspace Dashboard
- **/**: Dashboard home
- **/analytics**: Metrics and charts
- **/customers**: Customers list
- **/customers/:userId**: Customer detail
- **/announcements**: Workspace announcements
- **/status**: Status Center (API + Discord/Bot)
- **/documentation**: Documentation
- **/obfuscator**: Script obfuscation tools
- **/referrals**: Referral program
- **/workspace/:workspaceId**: Workspace detail
- **/workspace/:workspaceId/product/:productId**: Product detail (workspace view)

### Subscription / Billing
- **/subscription/manage**: Manage subscription
- **/subscription/success**: Subscription success
- **/subscription/cancel**: Subscription cancel

### Messaging / Inbox
- **/messages**: Conversations list
- **/messages/:conversationId**: Conversation detail

### Admin Console
- **/admin**: Admin home
- **/admin/users**: User management
- **/admin/workspaces**: Workspace management
- **/admin/products**: Product management
- **/admin/licenses**: License management
- **/admin/coin-shop**: Coin shop management
- **/admin/broadcast**: System broadcast
- **/admin/announcements**: Admin announcements
- **/admin/status-updates**: Status updates
- **/admin/notifications**: Notification settings

### User Settings
- **/user/settings**: Account settings

### Customer Mode
- **/**: Customer dashboard
- **/support**: Customer support page
- **/messages**: Customer messages
- **/messages/:conversationId**: Customer conversation detail
- **/user/settings**: Customer settings

---

## 2) Common Tasks (Step‑By‑Step)

### Create a Product
- **Products** → **New Product**
- Fill name, price, description
- Save as **Draft** or **Active**

### Publish/Hide a Product
- **Products** → open product → set **Active** or **Archived**

### View a Customer
- **Customers** → select user
- See purchases, licenses, and status

### Reset API Key
- **Settings** → **API Keys**
- Click **Regenerate**
- Old key invalidates immediately

### Manage Subscription
- **Subscription** → **Manage**
- Open billing portal and update payment info

---

## 3) Public vs Workspace vs Admin Views

### Public Pages
- Marketplace and product pages are **public** (no login required)
- Never show owner/admin‑only data

### Workspace Pages
- Only accessible after login
- Scoped to selected workspace

### Admin Pages
- Admin‑only; for moderation and global operations

---

## 4) Billing & Payments

- Stripe is the only supported checkout.
- If checkout fails: ask for error code + last 4 digits.
- Subscription success/cancel handled by `/subscription/*` routes.

---

## 5) Troubleshooting Guide

### “Product not visible”
- Check product is **Active**
- Confirm correct workspace
- Confirm user is on public marketplace page

### “Checkout failed”
- Ask for Stripe error code
- Check browser + device
- Verify Stripe status page

### “Image not loading”
- Ask for the image URL
- Confirm proxy is enabled and used

### “Access denied (403)”
- Confirm user role in workspace
- Ask for Discord role

### “Webhook not firing”
- Verify webhook URL
- Check webhook logs
- Confirm event type is enabled

---

## 6) Security & Alerts

- Security Alerts for suspicious scripts or content
- Manual Review requires staff approval before visible

---

## 7) AI Response Style

- Provide one clear next step
- Ask one clarifying question if missing key info
- Do not mention AI to customers unless configured

---

## 8) Escalation Rules

- System‑wide issue → notify staff lead
- Billing outages → check Stripe status
- Auth issues → request timestamps + screenshots
