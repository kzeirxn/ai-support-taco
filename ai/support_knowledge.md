
🧠 Evalyn – AI Knowledgebase
📌 Metadata
Version: 2.0
Last Updated: 2026-11-06
Description: Support knowledgebase for Evalyn, Taco's staff management platform for Roblox groups. Replaces the Taco Licensing knowledgebase. Licensing is no longer supported.
🧾 What is Evalyn?
Evalyn is a free staff management platform for Roblox groups: applications, shifts, sessions, moderation, activity tracking and Open Cloud integration in one place.

Dashboard: https://evalyn.tacogroup.uk
Guides: https://evalyn.tacogroup.uk/docs
In-app support tickets: https://evalyn.tacogroup.uk/tickets
Taco Licensing
Taco Licensing (licenses, products, TacoCoins) is no longer supported. Staff can't restore licensing accounts or features. Offer help with Evalyn instead.

🔐 Signing in & Accounts
How do I sign in?
Three ways on the sign-in page:

Continue with Discord
Continue with Roblox
Sign in with PIN: fast access without Discord or Roblox, for staff who have set a 6-digit PIN in a workspace. Search for the workspace, then enter the PIN.
Linking accounts
Account → Linking (/account/linking) connects Discord and Roblox to one Evalyn account.

Discord is needed for bot commands, DMs and notifications. Without it Evalyn can't reach you, and features that DM will say so.
Roblox is needed to track shifts and receive group role changes. Linking requires verifying with a code.
"This Discord account is already linked to another user" means that Discord is on a different Evalyn account. Sign in with it and unlink it there, or contact staff.
Unlinking Roblox means verifying again to relink. Workspaces that require a linked account lock until you do.
Sign-in history & security
Account → Security (/account/security) shows your last 25 sign-in attempts and how you log in.

Multiple accounts
You can stay signed in to several Evalyn accounts and switch between them. Signing out only signs out the current account.

Suspended accounts
A suspended account can't sign in. The sign-in page says the account is suspended.
They can ask the team to lift it at /suspended/appeal.
One pending appeal at a time. Appeals are 30 to 2000 characters.
After a denial they can see the team's reply and must wait 30 days before appealing again.
The AI can't lift or explain suspensions. Staff decide.
🧩 Workspaces
What is a workspace?
A workspace is one Roblox group on Evalyn. It holds that group's staff, roles, shifts, sessions, moderation, forms and settings.

Creating a workspace
Sign in, then enter your Roblox group id.
You must be rank 250+ in the group.
The group id becomes the address: group 1234567 lives at /w/1234567. It can't be changed, so links stay permanent.
Creating it pulls in the group's name, icon and member count.
An Open Cloud key is optional. It's only needed for Evalyn to change things in the group (rank changes, game syncing), not to read it.
First things to set up
Settings → General → Time zone. Set this before scheduling anything. Recurring sessions, quota periods and activity resets use it.
Developer → Scripts & Modules → Generate script (the Roblox handler).
Settings → Discord (connect a bot).
Settings → Access → Invitations (create a join link, or sync members from the group).
Settings → Features switches whole areas on and off. Turning one off hides it for everyone and removes its Discord commands, but deletes nothing. Switching it back restores it.
Sidebar
Pages are grouped as: Dashboard, People (Staff, Teams, Logbook, Recommendations), Insights (Activity, Performance, Leaderboard, Quotas, Reports), Operations (Shifts, Sessions, Calendar), Moderation (Live Panel, Bans, Chat Logs, Games), Communications (Announcements, Group Wall, Messages, Mail), Forms & Requests (Forms, Notices, Approvals), Resources (Knowledge, Documents, Policies, Alliances), Configuration (Settings, Audit Log, API Keys, Scripts & Modules, Configs, External Rankings).

A page missing from the sidebar usually means the feature is switched off in Settings → Features, or the user's role lacks the permission to view it.

Workspace tiers
Standard, Verified, Partner and Master. Some tiers include priority support. Owners can request verification from workspace settings. Staff decide.

Workspace suspension
A suspended workspace is restricted until platform staff restore it. Only staff can lift it.

🎮 The Roblox handler script
A server script in your game that talks to Evalyn. Without it, Evalyn knows nothing about what happens in-game.

It handles
Shifts (clock in/out)
Bans and blacklists (enforced on join)
Activity tracking, including AFK detection
Chat logging
In-game commands: /ban, /warn, /blacklist, /status
Installing
Developer → Scripts & Modules → Generate script
Paste it into a Script in ServerScriptService
Roblox Studio → Game Settings → Security → Allow HTTP Requests on
Important
Generating a new script invalidates the old one. Every game still running an old copy stops reporting until updated.
Only the workspace owner can generate it.
The script is a copy, not a live connection. New handler features need a regenerate and reinstall.
Not working?
Nothing arrives: Allow HTTP Requests is off. This is the usual cause.
Worked, then stopped: the script was regenerated somewhere else. Reinstall the current one.
Shifts work but chat logs are empty: older handlers only listened to legacy chat. Regenerate and reinstall.
🤖 Discord
Two ways to connect (only one is needed)
Official Evalyn bot (simplest):

Settings → Discord → Server / Guild: set your server id first. Linking without it does nothing.
Official Evalyn Bot → Link this workspace
Add to server and authorise it
It only registers commands for features that are switched on.

Your own bot: Settings → Discord → Discord Bot, paste a bot token and set the server id. Your own bot takes precedence over the official one.

Bot shows "Not connected"
The panel shows the reason. Most often the bot needs Server Members Intent enabled in the Discord developer portal.

Which channel gets what
Notification Channel: general events (forms, logbook, bans, announcements)
Activity Feed Channel: the same general events, plus shift start/end
Welcome Channel: new member welcomes only
Session board channels: per session type, in Settings → Features → Sessions
If both Notification and Activity Feed are set, events appear in both. That's intended. Clear one to get events in one place. Setting both to the same channel sends once.

Finding ids
Discord → Settings → Advanced → Developer Mode, then right-click a server, channel or role → Copy ID. Evalyn only accepts ids, not names.

👥 Roles & Permissions
If the Roblox group is connected, a member's rank is their group rank.
Members can hold additional roles inside Evalyn. Their effective rank is the highest. Removing an extra role drops them back to their group rank.
Settings → Access → Custom roles defines what each role can do.
A role can have a promotion ceiling limiting how high its holders can promote others.
Articles, folders and categories can have their own access rules, like a rank floor.
With Approvals on, some actions (like rank changes) wait for a reviewer.
"Permission denied" / can't see a page
The user's role lacks that permission, or the feature is switched off. A workspace admin fixes this in Settings → Access → Custom roles or Settings → Features. Evalyn support staff can't change a workspace's roles for them.

Platform staff in a workspace
Evalyn staff can open a workspace to help with support. A banner says so and every visit is recorded. They aren't members and aren't counted as such.

⏱️ Shifts
Clock in from the game (the handler, accurate) or from the dashboard (Shifts page button).
Duration runs from when they joined the game to clock-out, stored in whole minutes. A shift under a minute records as zero, which is correct.
AFK time still belongs to the shift but is marked.
Shifts left running (game crash) are closed automatically after an implausibly long time.
"4m" means four minutes, "4h" four hours. A few minutes shown as that many hours is a display bug, not bad data.
📅 Sessions
A session is any scheduled event: shift, training, meeting, raid.

Settings → Features → Sessions defines your own types, roles (with slots) and statuses.
A status label can be renamed freely. Its behaviour (Upcoming, Running now, Finished, Called off) is what Evalyn acts on.
Scheduled, In Progress and Completed can be renamed but not deleted.
One person holds one role per session. Unclaim the first role to take another.
Claiming a role means helping run it. Registering means attending. You can do both.
Times are entered in your own local time and shown correctly to everyone in their own timezone.
Managers (manage_sessions) can remove people from slots and change status. Subscribers get a DM on status changes.
📊 Activity, Quotas & Leaderboard
Activity: what the handler reports (joins, leaves, AFK), plus Discord message counts if enabled.
Quotas: requirements (shift hours, sessions, messages) per period, evaluated on a schedule. Evalyn never demotes anyone automatically.
Leaderboard: ranked by shift hours, sessions attended or activity hours, with optional rank and time filters.
Leave of absence: excludes someone from quota failure. It can tag them in Discord with a role and nickname prefix, reapplied on a schedule.
🛡️ Moderation
Bans stop someone entering your games. Issued from the dashboard or in-game /ban. Unbanning is recorded, not deleted. Can sync to Discord if enabled.
Blacklists are checked on join alongside bans (/blacklist).
Logbook: promotions, warnings, commendations and notes on a person. Entries can require acknowledgement.
Notices: workspace-wide statements like suspensions, leave or formal warnings.
✉️ Evalyn Mail
Free for every workspace.
Use an Evalyn subdomain address, or a custom domain after DNS verification.
Disabling mail never deletes mailboxes.
🎫 Getting help
Guides: /docs (getting started, Discord, handler, sessions, shifts, roles, moderation).
Help articles: /knowledge.
In-app tickets: /tickets. Tied to the Evalyn account, with a notification on each reply.
Screenshots and exact steps help staff answer in one go.
❗ Troubleshooting
Can't sign in
Try the other method (Discord or Roblox) linked to the same account.
"Account suspended" → appeal at /suspended/appeal.
Signed up with a different Discord? They may have two Evalyn accounts. Staff can check.
Group/workspace not showing
Check they're signed in to the right Evalyn account.
To create one they need rank 250+ in the group.
They may own it but not be a member. Staff can see this.
Shifts or activity not tracking
The handler isn't installed, Allow HTTP Requests is off, or the script was regenerated.
The staff member's Roblox account isn't linked.
Discord bot not responding
Server id not set in Settings → Discord.
Server Members Intent off (own bot).
The feature for that command is switched off.
Rank changes not applying in Roblox
An Open Cloud key is needed for Evalyn to change ranks.
With Approvals on, the change may be waiting for review.
Never ask for secrets
Passwords, PINs, Open Cloud keys, bot tokens and handler secrets must never be posted in a ticket. If one was posted, tell the user to rotate it.

🏁 Summary
Evalyn is a free, multi-workspace staff management platform for Roblox groups: staff, ranks and roles, shifts and activity, sessions, moderation, Discord integration and forms, one workspace per Roblox group.

