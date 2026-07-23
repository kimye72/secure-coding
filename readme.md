# Secure Coding

## Tiny Secondhand Shopping Platform.

You should add some functions and complete the security requirements.

## requirements

if you don't have a miniconda(or anaconda), you can install it on this url. - https://docs.anaconda.com/free/miniconda/index.html

```
git clone https://github.com/ugonfor/secure-coding
conda env create -f enviroments.yaml
```

## usage

run the server process.

```
python app.py
```

if you want to test on external machine, you can utilize the ngrok to forwarding the url.
```
# optional
sudo snap install ngrok
ngrok http 5000
```

## Configuration

- `SECRET_KEY` is required.
- how to generate a random local value using:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

- an example of setting it temporarily in WSL:

```bash
export SECRET_KEY='generated-value'
```

- `SESSION_COOKIE_SECURE=false` for local HTTP
- `SESSION_COOKIE_SECURE=true` for an HTTPS production deployment
- never commit `.env` or real secrets