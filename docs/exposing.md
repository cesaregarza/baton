<a name="reaching-a-baton-host-from-other-machines"></a>

# Connecting from another machine

By default, `baton serve` listens on `127.0.0.1:8765`, so only the host machine can connect. To reach it
from elsewhere, put an HTTPS reverse proxy or tunnel in front of it and set `host.public_url` to the
address clients will use. The MCP endpoint is `<public_url>/mcp`.

The examples below use placeholder names. Set up access and credentials before sharing a real address.
This guide covers the MCP host; the [dashboard and browser seat](people.md) have their own access rules.

## Choose the public address

A dedicated host name at the root, such as `https://baton.example.com`, is the simplest setup. OAuth
clients discover metadata under `/.well-known/` on that host. Set the exact external URL, including its
scheme and any non-default port:

```toml
[host]
public_url = "https://baton.example.com"
listen_host = "127.0.0.1"
listen_port = 8765
```

The address is also Baton's OAuth issuer, and its `/mcp` URL is the token audience. The MCP endpoint
checks the Host header against the configured name or loopback. A client that remembers an OAuth issuer
needs a stable name across restarts.

Keep the listener on loopback when using a local proxy or tunnel. Baton trusts `X-Forwarded-For` only from
a loopback peer, and uses it only for rate limiting. HTTPS protects bearer tokens as they travel between
clients and the proxy.

If you must use a path prefix such as `https://example.com/baton`, configure the proxy to forward
`/.well-known/oauth-authorization-server/baton` to Baton's root
`/.well-known/oauth-authorization-server` route. Baton serves the metadata at its own root route, not
natively at the path-form route. Other root metadata routes also need to reach the host. Using a dedicated
host name avoids this extra routing.

<a name="reverse-proxy-caddy-nginx"></a>

## Use a reverse proxy

Caddy can obtain and renew a certificate for a public name:

```caddy
baton.example.com {
    reverse_proxy 127.0.0.1:8765 {
        flush_interval -1
    }
}
```

With nginx, configure a certificate and disable buffering so MCP responses can stream:

```nginx
server {
    listen 443 ssl;
    server_name baton.example.com;
    # Configure ssl_certificate and ssl_certificate_key here.

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_buffering off;
        proxy_read_timeout 60s;
    }
}
```

The read timeout is longer than the MCP `wait` limit of 30 seconds. Set `public_url` as shown above and
restart Baton after changing its config.

<a name="tunnel-no-open-ports"></a>

## Use a tunnel

A tunnel service such as Cloudflare Tunnel or ngrok can give the host a public HTTPS name without opening
a port on the host machine. Forward it to `127.0.0.1:8765`, keep the external name stable, and set
`public_url` to its HTTPS URL.

A public tunnel makes the host reachable from the internet. Use long random bearer tokens or OAuth with a
strong consent passphrase, and keep OAuth redirect URIs restricted to the clients you intend to allow.

<a name="tailnet-or-vpn-private"></a>

## Use a private network

On a private network such as WireGuard or Tailscale, serve HTTPS on the private name with the network's
certificate feature or a reverse proxy, and set `public_url` to that name. Access is then limited to
machines that can reach that network and have a valid Baton credential.

On a fully trusted VPN, an operator may instead bind Baton to its private address and use an HTTP
`public_url`. In that setup the VPN's encryption, rather than TLS at Baton, protects credentials and
messages in transit. Evaluate access to the whole network before choosing it.

<a name="checking-it"></a>

## Check the connection

From a machine that should be able to reach the host:

```sh
# With OAuth enabled, inspect the protected-resource metadata.
curl -s https://baton.example.com/.well-known/oauth-protected-resource/mcp

# An unauthenticated request to the MCP endpoint should return 401.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://baton.example.com/mcp
```

Then connect a client with its own credential and try a harmless read. For OAuth, configure the client's
exact documented redirect URI in Baton; callbacks are not matched by prefix or wildcard. See
[Security](../SECURITY.md#credentials-and-roles) for authentication and revocation.
