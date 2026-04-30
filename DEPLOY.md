# Deploy na AWS — Do Zero ao Automático

## Pré-requisitos
- Conta AWS ativa
- Repositório no GitHub com o código do jeff-bot
- `.env` preenchido localmente (baseado em `.env.example`)

---

## Etapa 1 — Criar a instância EC2

1. Acesse o [Console AWS](https://console.aws.amazon.com/) → **EC2** → **Launch Instance**
2. Configure:
   - **Nome:** `jeff-bot`
   - **AMI:** Ubuntu Server 22.04 LTS (Free Tier eligible)
   - **Tipo:** `t3.small` (mínimo recomendado — 2 vCPU, 2 GB RAM)
   - **Par de chaves:** crie um novo par, salve o arquivo `.pem` em lugar seguro
3. Em **Network settings** → Edit → adicione regras de entrada no Security Group:

   | Tipo | Porta | Origem |
   |------|-------|--------|
   | SSH | 22 | Seu IP (ou `0.0.0.0/0` temporariamente) |
   | Custom TCP | 8000 | `0.0.0.0/0` |
   | Custom TCP | 5173 | `0.0.0.0/0` |

4. **Launch Instance** e aguarde ficar `Running`
5. Anote o **IP público** da instância (ex: `54.123.45.67`)

---

## Etapa 2 — Conectar na EC2 e instalar Docker

```bash
# No seu terminal local
chmod 400 ~/Downloads/jeff-bot.pem
ssh -i ~/Downloads/jeff-bot.pem ubuntu@<IP_EC2>
```

Dentro da EC2:

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker ubuntu
newgrp docker

# Verifique
docker --version
docker compose version
```

---

## Etapa 3 — Gerar chave SSH para o GitHub Actions

Esta chave vai permitir que o GitHub Actions entre na sua EC2 sem senha.

Rode **dentro da EC2**:

```bash
ssh-keygen -t ed25519 -C "github-actions" -f ~/.ssh/github_actions
# Quando pedir passphrase: deixe em branco (Enter)

# Autoriza a chave a acessar esta máquina
cat ~/.ssh/github_actions.pub >> ~/.ssh/authorized_keys

# Exibe a chave PRIVADA — copie tudo (inclusive as linhas -----BEGIN e -----END)
cat ~/.ssh/github_actions
```

Guarde esse conteúdo — você vai usá-lo no próximo passo.

---

## Etapa 4 — Clonar o repositório e configurar o .env

Ainda dentro da EC2:

```bash
git clone https://github.com/<seu-usuario>/<seu-repo>.git ~/jeff-bot
cd ~/jeff-bot

cp .env.example .env
nano .env
```

Preencha todas as variáveis no `.env`:

```env
DISCORD_USER_TOKEN=     # token da sua conta Discord (userbot)
DISCORD_BOT_TOKEN=      # token do bot oficial (Developer Portal)
DEEPSEEK_API_KEY=       # chave da API DeepSeek
NOTION_API_KEY=         # (opcional) integração Notion
NOTION_DATABASE_ID=     # (opcional)
AUTO_REPLY_ENABLED=false
```

Salve com `Ctrl+O`, saia com `Ctrl+X`.

---

## Etapa 5 — Primeira subida manual

```bash
cd ~/jeff-bot
docker compose up --build -d
docker compose ps   # todos devem mostrar "Up"
```

Teste:

```bash
curl http://localhost:8000/health
# Retorno esperado: {"ok": true}
```

---

## Etapa 6 — Configurar os secrets no GitHub

No repositório GitHub: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Crie os três secrets abaixo:

| Secret | Valor |
|--------|-------|
| `EC2_HOST` | IP público da instância (ex: `54.123.45.67`) |
| `EC2_USER` | `ubuntu` |
| `EC2_SSH_KEY` | Conteúdo completo do arquivo `~/.ssh/github_actions` (chave **privada**) |

---

## Etapa 7 — Testar o deploy automático

Faça qualquer alteração no código e faça push para a branch `main`:
teste
```bash
git add .
git commit -m "test: primeiro deploy automático"
git push origin main
```

Acompanhe em **GitHub → aba Actions** → workflow **"Deploy para EC2"**.

O job deve:
1. Conectar na EC2 via SSH
2. Fazer `git fetch + reset --hard` para sincronizar o código
3. Rebuildar e subir os containers com `docker compose up --build -d`

Após o job ficar verde, verifique:

```bash
# Direto do seu terminal local
curl http://<IP_EC2>:8000/health
# Retorno: {"ok": true}
```

---

## Comandos úteis na EC2

```bash
# Ver logs em tempo real
docker compose -f ~/jeff-bot/docker-compose.yml logs -f

# Ver logs de um container específico
docker compose -f ~/jeff-bot/docker-compose.yml logs -f bot

# Reiniciar um serviço
docker compose -f ~/jeff-bot/docker-compose.yml restart bot

# Parar tudo
docker compose -f ~/jeff-bot/docker-compose.yml down
```
