# Phone link setup (Telegram bot)

Do these in order. One step per line.

## On your phone
1. Install Telegram from the app store.
2. Sign in with your phone number.
3. Open a chat with `@BotFather` (blue tick).
4. Send `/newbot`.
5. Give it a display name, e.g. `AIDA`.
6. Give it a username ending in `bot`, e.g. `my_aida_private_bot`.
7. Copy the token BotFather sends (long string with a colon). Treat it like a password.
8. Open a chat with `@userinfobot`.
9. Send it any message.
10. Note the number after `Id:`. That is your user ID.
11. Open your new bot's chat and press Start.

## On the PC
12. Create the file `state\phone_secrets.env` inside your AIDA folder (default `%USERPROFILE%\AIDA`).
13. Put two lines in it, no quotes:
    `TELEGRAM_BOT_TOKEN=<token from step 7>`
    `TELEGRAM_ALLOWED_USER_ID=<number from step 10>`
14. Save. (It is gitignored.)
15. In `config.yaml` change `phone_link_enabled: false` to `phone_link_enabled: true`.
16. Restart the assistant (close window, run `start.bat`).
17. Look for `phone link running` in the log.

## Test
18. Message the bot: `what time is it`.
19. Message `/status`.
20. Message `/remind in 2m test`.
21. Message `delete everything` and check it refuses.

## Tidy-up
22. In BotFather send `/setjoingroups`, pick your bot, choose Disable.
23. If the token ever leaks: BotFather, `/revoke`, then update the file in step 13.

## Notes
- Nothing is opened on your router or PC. It only makes outbound HTTPS calls to Telegram.
- Telegram bot chats are not end-to-end encrypted. Don't send passwords, customer or work data.
- The PC must be on and the assistant running for replies.
- Tests: `.venv\Scripts\python.exe -m unittest test_phone_link -v`
