# Taco Support Dashboard Knowledge

## Overview
- This document is used by the AI assistant to answer questions about the Taco Support dashboard.
- If unsure, the AI should ask a clarifying question and avoid guessing.

## Login & Access
- Users sign in with their Discord account.
- Staff roles determine which sections are visible.
- If access is missing, ask for the user’s Discord role or staff role.

## Main Navigation
- Dashboard: Overview metrics and recent activity.
- Tickets/Threads: Active support conversations.
- Users: Customer profiles and history.
- Products/Marketplace: Product listings and detail pages.
- Settings: Workspace settings and integration keys.

## Common Tasks

### Reset an API Key
- Go to Settings → API Keys
- Click Regenerate
- The old key becomes invalid immediately

### Create a Product
- Go to Products → New Product
- Enter name, price, and description
- Save to publish

### Manage Listings
- Products can be Active or Draft
- Drafts are not visible publicly
- Image uploads appear after processing (proxy may be used)

### View a User
- Search Users by Discord ID or username
- Open profile to see purchases and history

## Billing & Payments
- Stripe is the only supported checkout.
- If checkout fails: ask for the error code and browser console details.

## Troubleshooting
- Missing data: Ask which workspace they are in.
- CORS image errors: The proxy endpoint should be used.
- 403 errors: Likely permission/role related.

## Escalation
- For system-wide outages, notify staff lead and provide time and error details.
