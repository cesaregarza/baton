# Reaching a baton host from other machines

`baton serve` listens on `127.0.0.1` by default. To reach it from elsewhere, put something in front of it
that terminates TLS. Then set `host.public_url` to the URL clients will use, without `/mcp`. Three common
setups follow.

These rules apply to all of them:

- **Serve baton at the root of its own host name**, such as `https://baton.example.com`. OAuth clients
  look for metadata at `/.well-known/...` on the host itself. With a path prefix such as `/baton`, those
  root paths would have to be forwarded separately.
- **Keep `listen_host = "127.0.0.1"`** and let the proxy or tunnel connect locally. The host trusts
  `X-Forwarded-For` only from a loopback peer, and uses it only for rate limiting.
- **`public_url` must match exactly** what clients use, scheme and port included. It is the OAuth issuer,
  and `<public_url>/mcp` is the token audience. The MCP endpoint also refuses requests whose `Host` header
  is not that name (or loopback), which blocks DNS rebinding.
- **Use HTTPS** whenever the connection leaves the machine. Tokens travel in the `Authorization` header.

## Reverse proxy (Caddy, nginx)

Caddy obtains and renews the certificate itself:

```
baton.example.com {
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1      # stream responses (MCP may use server-sent events)
    }
}
```

nginx:

```nginx
server {
    listen 443 ssl;
    server_name baton.example.com;
    # ssl_certificate ... ;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_buffering off;          # stream responses
        proxy_read_timeout 60s;       # longer than the 30 s maximum wait
    }
}
```

Then set:

```toml
[host]
public_url = "https://baton.example.com"
```

## Tunnel (no open ports)

A tunnelling service such as Cloudflare Tunnel or ngrok gives you a public HTTPS name that forwards to
`127.0.0.1:8765`. Point the tunnel at the local port, keep the host name stable across restarts (OAuth
clients remember the issuer), and set `public_url` to the tunnel's HTTPS URL.

A tunnel makes the host reachable from the whole internet. Use long random bearer tokens, or OAuth with a
strong passphrase, and keep `redirect_uris` tight.

## Tailnet or VPN (private)

On a private network such as WireGuard or Tailscale, only machines on that network can reach the host.
Either:

- serve HTTPS on the private name with the network's own certificate feature, or a reverse proxy as above,
  and set `public_url` to that name; or
- for a fully trusted network, set `listen_host` to the host's private address and `public_url` to
  `http://<private-address>:8765`. Tokens are then protected only by the VPN's own encryption.

This is the simplest way to let agents on several of your own machines share one host, without exposing
anything to the internet.

## Checking it

```sh
curl -s https://baton.example.com/.well-known/oauth-protected-resource/mcp   # with OAuth on
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://baton.example.com/mcp   # 401 without a token
```
