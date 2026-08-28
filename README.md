# calsync - umeos.ru schedule to .ics, served over HTTPS by Caddy.

Schedule is fetched with a public POST to
https://umeos.ru/blocks/umerasp/json_rasper.php

Past lessons in the .ics are kept, future ones are refreshed.

DEPLOY
```bash
curl -fsSL https://raw.githubusercontent.com/Speedware/calsync/main/deploy/install.sh | sudo bash
```

Asks for: domain, refresh interval (minutes), then group + slug pairs
until the group field is left blank. Installs everything and starts.

Subscription URL:  https://<domain>/cal/<slug>.ics

CONTROL

    calsync list                 groups and links
    calsync add <group> <slug>   add a group
    calsync rm <slug>            remove a group
    calsync update               refresh now
    calsync interval <minutes>   change refresh period (min 60)
    calsync logs                 service logs
    calsync upgrade              git pull + restart