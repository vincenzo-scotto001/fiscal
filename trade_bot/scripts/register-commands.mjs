// Registers the slash commands in src/commands.js with Discord for the Fiscal server.
// Server-level commands show up right away (global ones can take up to an hour).
import { COMMANDS } from "../src/commands.js";

const { DISCORD_APP_ID, DISCORD_GUILD_ID, DISCORD_BOT_TOKEN } = process.env;
for (const [name, value] of Object.entries({ DISCORD_APP_ID, DISCORD_GUILD_ID, DISCORD_BOT_TOKEN })) {
  if (!value) {
    console.error(`Missing ${name}`);
    process.exit(1);
  }
}

const r = await fetch(`https://discord.com/api/v10/applications/${DISCORD_APP_ID}/guilds/${DISCORD_GUILD_ID}/commands`, {
  method: "PUT",
  headers: { Authorization: `Bot ${DISCORD_BOT_TOKEN}`, "Content-Type": "application/json" },
  body: JSON.stringify(COMMANDS),
});
if (!r.ok) {
  console.error(`Discord rejected the commands (HTTP ${r.status}): ${(await r.text()).slice(0, 500)}`);
  process.exit(1);
}
console.log(`Registered ${COMMANDS.length} commands: ${COMMANDS.map((c) => `/${c.name}`).join(", ")}`);
