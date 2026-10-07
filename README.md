# ESP Monitor

Monitor serial com interface gráfica (PySide6) para os logs de placas ESP32 com ESP-IDF. Ele exibe, filtra e grava tudo que a placa envia pela USB/serial, usando as mesmas cores do `esp_log`.

| Nível | Cor | Origem |
|---|---|---|
| `E` Erro | vermelho | ANSI 31 |
| `W` Aviso | amarelo | ANSI 33 |
| `I` Info | verde | ANSI 32 |
| `D` Debug / `V` Verbose | sem cor (texto padrão) | como no ESP-IDF |
| `-` Outros | texto padrão ou a cor ANSI da própria linha | ROM/bootloader, `printf`, etc. |

## Instalação

Requer Python 3.10+ no Windows ou no Linux.

```bash
cd ESP-Monitor
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux: source .venv/bin/activate
pip install -r requirements.txt
```

No Linux, o usuário precisa ter acesso à porta. Em geral basta `sudo usermod -aG dialout $USER` e fazer logout e login.

Os testes do parser só precisam de `pytest` (não usam Qt nem porta serial).

## Uso

```bash
python ESP_monitor.py                         # escolhe a porta na interface
python ESP_monitor.py --port COM5             # Windows
python ESP_monitor.py --port /dev/ttyUSB0     # Linux
python ESP_monitor.py caminho/para/arquivo.log   # reabre um log salvo
```

O padrão é **115200 baud** (`CONFIG_ESP_CONSOLE_UART_BAUDRATE` padrão do ESP-IDF). Por padrão, ao abrir a porta, DTR e RTS ficam desligados, como no `idf.py monitor`, então conectar o monitor **não reseta** a placa. Para ver o boot desde o início, marque **Resetar ESP32 ao conectar** (veja abaixo).

> Em placas com conversor USB-serial o console costuma ser a UART0; em chips com USB-Serial-JTAG nativo (ESP32-S3, C3, …) ele pode estar nessa porta. Use a porta em que os logs aparecem. Só um programa por vez pode abrir a porta, então feche o `idf.py monitor` antes.

**Portas:** a lista vem do `pyserial` e mostra `dispositivo — descrição`:
- Windows: `COM3`, `COM5`… (veja no Gerenciador de Dispositivos);
- Linux: `/dev/ttyUSB0` (conversor USB-serial) ou `/dev/ttyACM0` (USB-Serial-JTAG).

O botão ⟳ atualiza a lista. O campo é editável: também dá para digitar o nome da porta.

## Recursos

- **Filtro por nível**: checkboxes E/W/I/D/V/Outros, com contadores.
- **Filtro por TAG**:
  - A lista é montada automaticamente com as TAGs que aparecem (`update`, `uds`, `can`, `client`, `auth`, …), com contadores.
  - Os botões **Todas**, **Nenhuma** e **Só seleção** ajustam a lista de uma vez.
  - O campo de busca filtra a própria lista de TAGs.
- **Busca de texto ou regex** (`Ctrl+F`):
  - Destaca as linhas encontradas e navega entre elas com `F3` / `Shift+F3` (ou `Enter`).
  - "Mostrar só resultados" esconde as demais linhas.
  - Há opção para diferenciar maiúsculas.
- **Gravação contínua**:
  - Cada conexão gera um `ESP_AAAAMMDD_HHMMSS_<porta>.log` na pasta escolhida (padrão `~/ESP_logs`).
  - A gravação acontece na thread de leitura, com flush a cada lote, então nada se perde se a interface travar ou se o limite de memória for atingido.
- **Resetar ESP32 ao conectar**: com a opção marcada, o monitor reseta o ESP32 logo depois de abrir a porta, e o log começa no boot.
  - O reset é o mesmo "hard reset" do `esptool`: com DTR desligado (GPIO0 em nível alto, boot normal), o RTS (EN) fica ativo por 0,2 s.
  - Funciona no Windows e no Linux, tanto em placas com conversor USB-serial e circuito de auto-reset (CP210x, CH340, FTDI) quanto no USB-Serial-JTAG nativo (ESP32-S3, C3, C6, H2). No Windows, o DTR é reescrito a cada mudança do RTS, como faz o `esptool`, porque o driver `usbser.sys` só aplica o RTS junto com o DTR.
  - Só a primeira abertura reseta; as reconexões automáticas não.
  - Placas sem circuito de auto-reset (EN e GPIO0 sem ligação ao DTR e RTS) não resetam por essa opção.
- **Reconectar automaticamente**: se a placa reiniciar ou a USB cair, o monitor tenta reabrir a porta a cada 1 s e continua gravando no mesmo arquivo. Sem essa opção, a leitura termina no primeiro erro e o motivo aparece na barra de status.
- **Salvar** (`Ctrl+S`) salva tudo o que está em memória. **Salvar linhas visíveis** (`Ctrl+Shift+S`) salva só o resultado dos filtros.
- **Abrir log** (`Ctrl+O`): reabre um `.log` gravado e aplica os filtros de nível, TAG e busca atuais. Só funciona desconectado. Durante o carregamento, **Conectar**, **Abrir log** e **Limpar tela** ficam bloqueados.
- **Auto-scroll**: segue o fim do log enquanto a barra de rolagem estiver no final. Basta rolar para cima para "pausar".
- **Copiar** (`Ctrl+C`) as linhas selecionadas, sem a hora do PC. **Limpar tela** com `Ctrl+L` (não afeta a gravação). **Ir para o fim** com `Ctrl+End`. Passar o mouse sobre a mensagem mostra a linha completa.
- **Limite em memória** (padrão 200 000 linhas, ajustável de 10 000 a 5 000 000):
  - Acima dele, as linhas mais antigas saem da tela em blocos (o excesso mais 10 % do limite). A barra de status mostra quantas foram descartadas.
  - Elas continuam no arquivo da gravação contínua, que não tem limite.
  - Um limite menor só vale a partir do próximo lote recebido.
- As preferências (porta, baud, reset ao conectar, reconexão, pasta e opções de gravação, limite) são lembradas entre execuções, via `QSettings`: no registro do Windows (`HKCU\Software\ESP\ESP_monitor`) ou em `~/.config/ESP/ESP_monitor.conf` no Linux.

## Formato do arquivo `.log`

É texto puro em UTF-8, sem códigos ANSI. Cada linha recebe opcionalmente a hora do PC como prefixo:

```
[2026-10-03 14:05:31.559] I (312) main_task: Calling app_main()
[2026-10-03 14:05:31.561] E (900) metadata: Falha validando timestamp.json:
[2026-10-03 14:05:31.561]     VALIDATION_EXPIRED
```

A gravação contínua e o **Salvar** usam o mesmo formato (UTF-8, fim de linha LF). A opção **Incluir hora do PC no arquivo** vale para os dois.

## Classificação das linhas

- Linhas no formato `L (tempo) TAG: mensagem` são classificadas pelo nível e pela TAG. A TAG vai até o primeiro `:`, mesmo sem espaço depois (`wifi:...`).
- O tempo pode ser em ms desde o boot (`CONFIG_LOG_TIMESTAMP_SOURCE_RTOS`, o padrão) ou `hh:mm:ss.mmm`.
- Os códigos ANSI são removidos do texto exibido e gravado. Linhas sem nível que têm outra cor (ex.: ciano) mantêm a cor na tela.
- As linhas de continuação de mensagens multi-linha herdam o nível pela cor ANSI ativa.
- Linhas com `Guru Meditation Error`, `abort() was called`, `Backtrace:` e similares são marcadas como `E` com a TAG `PANIC`.
- Ao reabrir um `.log` (que não guarda cores), as linhas de continuação sem prefixo aparecem como "Outros". Um prefixo de hora inválido é mantido como parte do texto da linha.
- Uma linha parada sem `\n` por mais de 0,5 s (ex.: um prompt) é exibida sozinha. Se o resto chegar depois, a linha aparece partida em duas.

## Como funciona

| Arquivo | Papel |
|---|---|
| `ESP_monitor.py` | Janela (PySide6): barras de conexão e gravação, filtros, busca, tabela (`LogModel`/`LogTable`) |
| `serial_reader.py` | `SerialReader` (`QThread`): lê a porta, separa as linhas, faz o parse, grava o `.log` e envia lotes à janela |
| `log_parser.py` | Parser das linhas do `esp_log` (nível, tempo, TAG, cores ANSI, panic). Python puro, testado em `tests/` |

- Cada **Conectar** cria uma thread de leitura. Só ela acessa a porta e o arquivo de gravação.
- As linhas chegam à janela em lotes, no máximo a cada 50 ms, por sinais Qt. O Qt enfileira os sinais na thread da interface.
- Cada lote é gravado e recebe `flush` **antes** de ir para a tela. Por isso o limite em memória e um travamento da interface não afetam o `.log`. Se a porta cair, o último lote e a linha incompleta também são gravados antes da reconexão.
- **Desconectar** sinaliza a thread e espera até 3 s.

## Testes

```bash
pip install pytest
python -m pytest tests          # na raiz do repositório
```

Os testes cobrem só o `log_parser.py`. A interface e a leitura serial não têm testes automáticos.
