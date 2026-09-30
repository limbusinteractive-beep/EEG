# Muse 2 → OSC (ponte para PC)

Liga o Muse 2 diretamente ao PC e envia os dados por OSC **com os mesmos endereços do Mind Monitor**. Um patch de TouchDesigner que já recebe do Mind Monitor funciona sem alterações.

```
Muse 2 ──BLE──> muselsl ──LSL──> muse_bridge.py ──OSC/UDP──> TouchDesigner
```

## Instalação

1. Python 3.10–3.12.
2. Na pasta do projeto:
   ```
   pip install -r requirements.txt
   ```
3. Testar sem o sensor (o TouchDesigner deve receber dados na porta 5000):
   ```
   python muse_bridge.py --simulate
   ```

## Uso

| Comando | O que faz |
|---|---|
| `python muse_bridge.py` | Procura o Muse, liga e envia para `127.0.0.1:5000` |
| `start_bridge.bat` | O mesmo, com duplo clique (Windows); aceita os mesmos argumentos |
| `python muse_bridge.py --list` | Lista os Muses visíveis |
| `python muse_bridge.py --address 00:55:DA:...` | Liga a um Muse específico |
| `python muse_bridge.py --ip 192.168.1.20 --port 7000` | Envia para outra máquina/porta |
| `python muse_bridge.py --average` | Um valor por banda (média dos 4 sensores) |
| `python muse_bridge.py --bands absolute` | Só absolutas (como o Mind Monitor) |
| `python muse_bridge.py --no-raw` | Não envia EEG em bruto nem sensores (menos tráfego) |
| `python muse_bridge.py --no-stream` | Usa um stream LSL já aberto (ex.: BlueMuse) |
| `python muse_bridge.py --debug` | Imprime valores 1x/s para calibrar |
| `python osc_monitor.py` | Mostra o que chega à porta 5000 (fecha o TD primeiro, ou usa outra porta) |

Se a ligação cair (sensor retirado, fora de alcance, Bluetooth instável), a ponte reinicia sozinha. Em `/bridge/connected` recebes 1 ou 0.

## Endereços OSC

| Endereço | Valores | Taxa |
|---|---|---|
| `/muse/eeg` | TP9, AF7, AF8, TP10, AUX (µV) | 256 Hz |
| `/muse/acc` | x, y, z (g) | 52 Hz |
| `/muse/gyro` | x, y, z (°/s) | 52 Hz |
| `/muse/ppg` | 3 canais | 64 Hz |
| `/muse/elements/{delta,theta,alpha,beta,gamma}_absolute` | 4 floats (ou 1 com `--average`), log10 da potência | 10 Hz |
| `/muse/elements/{banda}_relative` | 4 floats, 0–1 (as 5 bandas somam 1) — **extra, não existe no Mind Monitor** | 10 Hz |
| `/muse/elements/horseshoe` | 4 floats: 1 bom, 2 médio, 4 mau | 10 Hz |
| `/muse/elements/touching_forehead` | 1 / 0 | 10 Hz |
| `/muse/elements/blink` | 1 quando deteta | evento |
| `/muse/elements/jaw_clench` | 1 quando deteta | evento |
| `/bridge/connected` | 1 / 0 | na ligação |

Bandas: delta 1–4, theta 4–8, alpha 7.5–13, beta 13–30, gamma 30–44 Hz. Janela de FFT de 1 s (Hann), atualizada a 10 Hz.

## Diferenças face ao Mind Monitor

- As bandas absolutas usam o mesmo conceito (log10 da potência), mas a escala pode não coincidir exatamente. Se o patch tiver ranges fixos num Math CHOP, confirma-os com `--debug` ou com o `osc_monitor.py`.
- O horseshoe, o piscar e o maxilar são calculados aqui por heurística e não pelo firmware da Muse. Os limiares por defeito foram testados só com dados simulados, por isso **calibra com o sensor real**:
  ```
  python muse_bridge.py --debug
  ```
  Olha para `sd` (contacto), `blink` (pico ao piscar) e `jaw` (ao apertar os dentes) e ajusta `--good`, `--medium`, `--blink-thresh` e `--jaw-thresh`. Se não precisares destes eventos, usa `--no-events`.
- A bateria não é enviada (o muselsl não a publica).

## Problemas comuns

- **Não encontra o Muse:** fecha o Mind Monitor no telemóvel (o Muse só aceita uma ligação), liga o sensor e corre `--list`.
- **Ligação instável no Windows:** o Bluetooth integrado dos portáteis costuma ser a causa. Um dongle BLE resolve na maior parte dos casos. Com um dongle BLED112, usa `--backend bgapi`. Em alternativa, usa o BlueMuse para ligar e a ponte com `--no-stream`.
- **O TouchDesigner não recebe:** confirma que o OSC In CHOP/DAT está na mesma porta e que nada mais a ocupa (o `osc_monitor.py` também a ocupa).
