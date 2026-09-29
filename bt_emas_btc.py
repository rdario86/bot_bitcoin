import ccxt
import pandas as pd
import math
from datetime import datetime
import pytz

# ==========================================
# 1. CONFIGURACIÓN DEL BACKTESTER
# ==========================================
SIMBOLO = 'BTC/USDT'  # Formato estándar para descargar historial
TIMEFRAME = '1m'
TOTAL_VELAS = 10000   # Cantidad de minutos a evaluar (~7 días de historial)

CAPITAL_INICIAL = 1000.0  # Capital simulado en USDT
RIESGO_PCT = 3.0          # Riesgo por trade (3%)
RATIO_RR = 2.0            # Ratio de beneficio (1:2)
COMISION_EXCHANGE = 0.0005 # Comisión simulada por transacción (0.05%)

tz_ny = pytz.timezone('America/New_York')

# Inicializar exchange (Binance es ideal para extraer grandes datos históricos públicos sin API Key)
exchange = ccxt.binance({'enableRateLimit': True})

# ==========================================
# 2. EXTRACCIÓN DE DATOS HISTÓRICOS
# ==========================================
def obtener_historial(simbolo, timeframe, total_velas):
    print(f"📥 Descargando {total_velas} velas históricas de {simbolo}...")
    ahora = exchange.milliseconds()
    desde = ahora - (total_velas * 60 * 1000)
    todas_las_velas = []
    
    while len(todas_las_velas) < total_velas:
        limite = min(1000, total_velas - len(todas_las_velas))
        try:
            velas = exchange.fetch_ohlcv(simbolo, timeframe=timeframe, since=desde, limit=limite)
            if not velas:
                break
            todas_las_velas.extend(velas)
            desde = velas[-1][0] + 60000  # Siguiente minuto
        except Exception as e:
            print(f"⏳ Error descargando: {e}")
            break
            
    df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
    df.set_index('Timestamp', inplace=True)
    df.index = df.index.tz_localize('UTC').tz_convert(tz_ny)
    df = df[~df.index.duplicated(keep='last')]
    
    # Calcular indicadores
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA55'] = df['Close'].ewm(span=55, adjust=False).mean()
    
    print(f"✅ Historial listo: {len(df)} velas obtenidas.")
    return df

# ==========================================
# 3. MOTOR DE SIMULACIÓN (BACKTESTING)
# ==========================================
def ejecutar_backtest(df):
    print("\n🚀 INICIANDO SIMULACIÓN DE BACKTESTING...\n")
    
    capital = CAPITAL_INICIAL
    trade_abierto = False
    estado_espera = None
    crossover_sl_ref = None
    tiempo_cruce = None
    
    # Variables de posición actual
    pos_tipo = None
    pos_entrada = 0.0
    pos_sl = 0.0
    pos_tp = 0.0
    pos_cantidad = 0.0
    
    # Estadísticas
    trades_totales = []
    ganados = 0
    perdidos = 0
    
    for i in range(2, len(df)):
        vela_actual = df.iloc[i]       # Simulando la vela que se está moviendo (usaremos sus High/Low para SL/TP)
        vela_cerrada = df.iloc[i-1]    # La última vela completamente cerrada
        vela_anterior = df.iloc[i-2]   # La vela anterior a la cerrada
        
        hora_actual = vela_actual.name.time()
        dia_semana = vela_actual.name.weekday()
        
        # --- 1. VERIFICAR CIERRE DE EMERGENCIA ---
        es_hora_cierre_manana = (hora_actual.hour == 11 and hora_actual.minute == 0)
        es_hora_cierre_noche = (hora_actual.hour == 23 and hora_actual.minute == 0)
        
        if trade_abierto and (es_hora_cierre_manana or es_hora_cierre_noche):
            precio_cierre = vela_actual['Open']
            pnl = 0
            if pos_tipo == 'Long':
                pnl = (precio_cierre - pos_entrada) * pos_cantidad
            else:
                pnl = (pos_entrada - precio_cierre) * pos_cantidad
                
            capital += pnl
            trades_totales.append({'Fecha': vela_actual.name, 'Tipo': pos_tipo, 'Resultado': 'Cierre Sesión', 'PnL': pnl, 'Capital': capital})
            trade_abierto = False
            estado_espera = None
            continue

        # --- 2. GESTIONAR POSICIÓN ABIERTA (Verificar SL / TP) ---
        if trade_abierto:
            # Por ser conservadores en el backtesting, si el High y el Low tocan tanto el SL como el TP, asumimos pérdida
            hit_sl = False
            hit_tp = False
            
            if pos_tipo == 'Long':
                if vela_actual['Low'] <= pos_sl: hit_sl = True
                elif vela_actual['High'] >= pos_tp: hit_tp = True
            elif pos_tipo == 'Short':
                if vela_actual['High'] >= pos_sl: hit_sl = True
                elif vela_actual['Low'] <= pos_tp: hit_tp = True
                
            if hit_sl:
                riesgo_perdido = (pos_entrada - pos_sl) * pos_cantidad if pos_tipo == 'Long' else (pos_sl - pos_entrada) * pos_cantidad
                capital -= riesgo_perdido
                perdidos += 1
                trades_totales.append({'Fecha': vela_actual.name, 'Tipo': pos_tipo, 'Resultado': 'SL', 'PnL': -riesgo_perdido, 'Capital': capital})
                trade_abierto = False
            elif hit_tp:
                beneficio = (pos_tp - pos_entrada) * pos_cantidad if pos_tipo == 'Long' else (pos_entrada - pos_tp) * pos_cantidad
                capital += beneficio
                ganados += 1
                trades_totales.append({'Fecha': vela_actual.name, 'Tipo': pos_tipo, 'Resultado': 'TP', 'PnL': beneficio, 'Capital': capital})
                trade_abierto = False
                
            if not trade_abierto:
                estado_espera = None
            continue # Si estábamos en trade y se evaluó, pasamos al siguiente minuto
            
        # --- 3. VERIFICAR VENTANAS OPERATIVAS ---
        es_lunes_a_viernes = dia_semana in [0, 1, 2, 3, 4]
        es_domingo_a_jueves = dia_semana in [6, 0, 1, 2, 3]
        en_manana = es_lunes_a_viernes and (pd.to_datetime('08:00').time() <= hora_actual <= pd.to_datetime('10:30').time())
        en_noche = es_domingo_a_jueves and (pd.to_datetime('20:00').time() <= hora_actual <= pd.to_datetime('22:30').time())
        
        es_hora_operativa = en_manana or en_noche
        
        # --- 4. BÚSQUEDA DE ENTRADAS ---
        if es_hora_operativa and not trade_abierto:
            
            # A. LÓGICA DE ESPERA
            if estado_espera == "Esperando_Long" and vela_cerrada.name > tiempo_cruce:
                if vela_cerrada['Close'] >= vela_cerrada['Open']: # Verde
                    entrada = vela_cerrada['Close']
                    sl = min(crossover_sl_ref, vela_cerrada['Low'])
                    tp = entrada + ((entrada - sl) * RATIO_RR)
                    ejecutar_trade('Long', entrada, sl, tp, capital)
                else:
                    estado_espera = None
                    
            elif estado_espera == "Esperando_Short" and vela_cerrada.name > tiempo_cruce:
                if vela_cerrada['Close'] <= vela_cerrada['Open']: # Roja
                    entrada = vela_cerrada['Close']
                    sl = max(crossover_sl_ref, vela_cerrada['High'])
                    tp = entrada - ((sl - entrada) * RATIO_RR)
                    ejecutar_trade('Short', entrada, sl, tp, capital)
                else:
                    estado_espera = None
                    
            # B. BUSCAR NUEVOS CRUCES
            elif estado_espera is None:
                cruce_alcista = (vela_anterior['EMA20'] <= vela_anterior['EMA55']) and (vela_cerrada['EMA20'] > vela_cerrada['EMA55'])
                cruce_bajista = (vela_anterior['EMA20'] >= vela_anterior['EMA55']) and (vela_cerrada['EMA20'] < vela_cerrada['EMA55'])
                
                if cruce_alcista:
                    if vela_cerrada['Close'] >= vela_cerrada['Open']:
                        entrada = vela_cerrada['Close']
                        sl = vela_cerrada['Low']
                        tp = entrada + ((entrada - sl) * RATIO_RR)
                        ejecutar_trade('Long', entrada, sl, tp, capital)
                    else:
                        estado_espera = "Esperando_Long"
                        crossover_sl_ref = vela_cerrada['Low']
                        tiempo_cruce = vela_cerrada.name
                        
                elif cruce_bajista:
                    if vela_cerrada['Close'] <= vela_cerrada['Open']:
                        entrada = vela_cerrada['Close']
                        sl = vela_cerrada['High']
                        tp = entrada - ((sl - entrada) * RATIO_RR)
                        ejecutar_trade('Short', entrada, sl, tp, capital)
                    else:
                        estado_espera = "Esperando_Short"
                        crossover_sl_ref = vela_cerrada['High']
                        tiempo_cruce = vela_cerrada.name

        # Función interna para simular la ejecución y guardar el estado
        def ejecutar_trade(tipo, entrada, sl, tp, cap_actual):
            nonlocal trade_abierto, pos_tipo, pos_entrada, pos_sl, pos_tp, pos_cantidad
            riesgo_precio = abs(entrada - sl)
            if riesgo_precio == 0: riesgo_precio = entrada * 0.0005
            
            riesgo_usd = cap_actual * (RIESGO_PCT / 100)
            cantidad = riesgo_usd / riesgo_precio
            
            # Descontar comisiones simuladas (Entrada)
            comision = (cantidad * entrada) * COMISION_EXCHANGE
            
            pos_tipo = tipo
            pos_entrada = entrada
            pos_sl = sl
            pos_tp = tp
            pos_cantidad = cantidad
            trade_abierto = True

    # ==========================================
    # 4. REPORTE FINAL DE RENDIMIENTO
    # ==========================================
    total_operaciones = ganados + perdidos
    win_rate = (ganados / total_operaciones * 100) if total_operaciones > 0 else 0
    beneficio_neto = capital - CAPITAL_INICIAL

    print("📊 ================= RESULTADOS DEL BACKTEST ================= 📊")
    print(f"💰 Capital Inicial: ${CAPITAL_INICIAL:.2f}")
    print(f"💵 Capital Final:   ${capital:.2f}")
    print(f"📈 Beneficio Neto:  ${beneficio_neto:.2f} ({(beneficio_neto/CAPITAL_INICIAL)*100:.2f}%)")
    print(f"🔄 Operaciones Totales: {total_operaciones}")
    print(f"✅ Ganadas: {ganados}")
    print(f"❌ Perdidas: {perdidos}")
    print(f"🎯 Win Rate: {win_rate:.2f}%")
    print("================================================================")
    
    # Retornar DataFrame de trades por si se quieren graficar o exportar a Excel
    return pd.DataFrame(trades_totales)

# ==========================================
# EJECUCIÓN PRINCIPAL
# ==========================================
if __name__ == "__main__":
    df_historico = obtener_historial(SIMBOLO, TIMEFRAME, TOTAL_VELAS)
    if not df_historico.empty:
        df_trades = ejecutar_backtest(df_historico)
        # Opcional: Exportar resultados a CSV
        # df_trades.to_csv('resultados_backtest.csv', index=False)
