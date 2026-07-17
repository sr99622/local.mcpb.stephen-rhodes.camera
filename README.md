<h2>MCP Runtime Example using libonvif</h2>

This example uses the UV runtime

Please note that you have to edit the json file manually. The file can be found using Claude Desktop. Go to File->Settings->Developer and click the Edit Config button. This will bring up a file browser highlighting the claude_dekstop_config.json file. Adjust these settings to your current situation and paste them into the json at the top.

you can get the git server from 

```
git clone https://github.com/modelcontextprotocol/servers.git
```

and the libonvif MCP at

```
git clone https://github.com/sr996222/local.mcpb.stephen-rhodes.camera
```

```
  "mcpServers": {
    "git": {
      "command": "uv",
      "args": [
        "--directory", 
        "C:\\Users\\sr996\\Projects\\servers\\src\\git",
        "run",
        "src\\mcp_server_git"
      ]
    },    
    "camera": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\Users\\sr996\\Projects\\local.mcpb.stephen-rhodes.camera\\src",
        "run",
        "camera.py"
      ],
      "env": {
        "CAMERA_USERNAME": "admin",
        "CAMERA_PASSWORD": "admin123",
        "STREAM_SERVER_IP": "10.1.1.13"
      }
    }
  },

```

## Known Issue: Extensions dashboard installer does not connect

**Status:** Open, unconfirmed by Anthropic as of this writing. Bug reports submitted
by the author and other users have not received a response.

**Summary:** Installing this server through Claude Desktop's Extensions dashboard
(the `.mcpb` installer, rather than manually editing `claude_desktop_config.json`)
consistently fails to actually connect, even though the install itself reports no
error.

**Observed behavior:**
- The extension installs without any error message.
- It appears in the Extensions dashboard as "Enabled," with all configured fields
  (Camera Username, Camera Password, Stream Server IP) correctly populated.
- The dashboard correctly lists every tool the server exposes by name (e.g. "Get
  camera mcp version," "Set camera video encoder," "Grep search") - meaning Claude
  Desktop successfully parses the extension's manifest/tool schema at install time.
- Despite this, none of the server's tools are ever actually callable in a
  conversation. They never appear as available tools, and a fresh restart of Claude
  Desktop does not change this.
- **Workaround:** manually adding an equivalent `mcpServers` entry directly to
  `claude_desktop_config.json` (Settings -> Developer -> Edit Config) and restarting
  Claude Desktop connects reliably every time. See the config example above.

**Reproduced independently on two separate machines:**
- Windows 11 (original environment) - installer path failed; manual config path
  worked.
- macOS, fresh install, current Claude Desktop version, no prior configuration or
  leftover state from the Windows machine - installer path failed the same way;
  manual config path worked immediately.

The consistency across two unrelated machines and operating systems, with a fresh
install on one of them, points to this being a genuine bug in the Extensions
dashboard's installer/connection process itself, rather than something
environment- or leftover-config-specific to the original (Windows) setup.

If you hit this: skip the dashboard installer and use the manual
`claude_desktop_config.json` approach documented at the top of this README instead.
