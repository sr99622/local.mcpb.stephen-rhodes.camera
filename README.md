<h2>MCP Runtime Example using libonvif</h2>

This example uses the UV runtime

Please note that the installer will only work partially, you have to edit the json file manually as before.

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
