# Appliance testing

Use a disposable Linux machine with Docker available to the test user. Run:

```sh
./appliance/tests/test-appliance.sh
./appliance/install.sh
curio health
```

Verify that resolver responses use the origin used for the request, that
`/ipfs` and `/arweave` are served through that origin, and that Kubo and AR.IO
gateway ports are not exposed directly. Exercise an authenticated static upload
and `POST /keep`; confirm the object remains after restarting Compose.

Do not test against a production Curio state tree.
