import ccxt
import pandas as pd
import time
from datetime import datetime
import pytz
import math 

# ==========================================
# 1. CONFIGURACIÓN DEL USUARIO
# ==========================================
API_KEY = 'AGREGAR API KEY'
API_SECRET = 'AGREGAR API SECRET'

SIMBOLO = 'BTC/USDT:USDT'
RIESGO_PCT = 3.0            # 🌟 Riesgo Fijo del 3% del capital disponible
APALANCAMIENTO_MAX = 125    # Límite máximo permitido por BingX para BTC
RATIO_RR = 2.0              # Ratio Beneficio (1:2)

# Conexión a BingX
exchange = ccxt.bingx({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

tz_ny = pytz.timezone('America/New_York')

# ==========================================
# 2. FUNCIONES PRINCIPALES
# ==========================================
def obtener_velas(simbolo, timeframe='1m', total_velas=1500):
    try:
        ahora = exchange.milliseconds()
        desde = ahora - (total_velas * 60 * 1000) 
        
        todas_las_velas = []
        
        while len(todas_las_velas) < total_velas:
            velas = exchange.fetch_ohlcv(simbolo, timeframe=timeframe, since=desde, limit=500)
            if not velas:
                break
            todas_las_velas.extend(velas)
            desde = velas[-1][0] + 60000 
            time.sleep(0.1) 
            
        if not todas_las_velas:
            return pd.DataFrame()
            
        df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        df.index = df.index.tz_localize('UTC').tz_convert(tz_ny)
        
        df = df[~df.index.duplicated(keep='last')]
        
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA55'] = df['Close'].ewm(span=55, adjust=False).mean()
        
        return df
    except Exception as e:
        print(f"⏳ Error descargando historial: {e}")
        return pd.DataFrame()

def ejecutar_orden(simbolo, tipo_trade, entrada, stop_loss, take_profit):
    try:
        balance_info = exchange.fetch_balance()
        balance_usdt = balance_info.get('USDT', {}).get('free', 0.0)
        
        if balance_usdt <= 0:
            print("❌ Error: No hay balance USDT disponible para operar.")
            return False

        # --- CÁLCULO DE RIESGO 3% EXACTO ---
        riesgo_precio = abs(entrada - stop_loss)
        
        if riesgo_precio == 0: 
            margen = entrada * 0.0005
            riesgo_precio = margen
            stop_loss = (entrada - margen) if tipo_trade == 'Long' else (entrada + margen)
        
        riesgo_usd = balance_usdt * (RIESGO_PCT / 100)
        
        cantidad_bruta = riesgo_usd / riesgo_precio
        cantidad = math.floor(cantidad_bruta * 10000) / 10000 
        
        if cantidad <= 0:
            print("❌ Error: Capital insuficiente para comprar el mínimo permitido de BTC.")
            return False
            
        # --- APALANCAMIENTO DINÁMICO ---
        valor_posicion = cantidad * entrada
        margen_utilizable = balance_usdt * 0.95
        
        apalancamiento_necesario = math.ceil(valor_posicion / margen_utilizable)
        
        if apalancamiento_necesario < 1:
            apalancamiento_necesario = 1
        elif apalancamiento_necesario > APALANCAMIENTO_MAX:
            apalancamiento_necesario = APALANCAMIENTO_MAX
            cantidad_maxima = (margen_utilizable * APALANCAMIENTO_MAX) / entrada
            cantidad = math.floor(cantidad_maxima * 10000) / 10000
            print(f"⚠️ SL muy ajustado. Apalancamiento topado a {APALANCAMIENTO_MAX}x. Se redujo la cantidad.")

        try:
            exchange.set_leverage(apalancamiento_necesario, simbolo)
            print(f"⚙️ Apalancamiento Dinámico ajustado a: {apalancamiento_necesario}x")
        except Exception as e:
            print(f"⚠️ Info: No se pudo cambiar apalancamiento automáticamente: {e}")

        # --- LANZAMIENTO DE ORDEN LIMIT (ENTRADA) ---
        lado_entrada = 'buy' if tipo_trade == 'Long' else 'sell'
        lado_salida = 'sell' if tipo_trade == 'Long' else 'buy'
        position_side = 'LONG' if tipo_trade == 'Long' else 'SHORT'
        
        print(f"💰 [CAPITAL] Balance: ${balance_usdt:.2f} USDT | Arriesgando: ${riesgo_usd:.2f} ({RIESGO_PCT}%)")
        print(f"🚀 Lanzando orden LIMIT {lado_entrada.upper()} a {entrada:.2f} por {cantidad} BTC...")
        
        orden = exchange.create_order(
            symbol=simbolo, type='LIMIT', side=lado_entrada, 
            amount=cantidad, price=entrada, params={'positionSide': position_side}
        )
        orden_id = orden.get('id')
        print(f"⏳ Orden LIMIT colocada. Esperando a que el mercado la tome (Máx 30s)...")
        
        orden_llenada = False
        for _ in range(15): # 15 comprobaciones de 2 segundos = 30 segundos
            time.sleep(2)
            try:
                info_orden = exchange.fetch_order(orden_id, simbolo)
                if info_orden['status'] == 'closed':
                    orden_llenada = True
                    break
                elif info_orden['status'] in ['canceled', 'rejected']:
                    print("⚠️ La orden LIMIT fue cancelada o rechazada por el exchange.")
                    return False
            except Exception:
                pass 
                
        if not orden_llenada:
            print("⏰ El precio se escapó. Cancelando la orden LIMIT para proteger el capital...")
            try:
                exchange.cancel_order(orden_id, simbolo)
            except Exception as e:
                print(f"⚠️ Error intentando cancelar la orden: {e}")
            return False
            
        print("✅ ¡Orden LIMIT ejecutada con éxito! Posición abierta.")
        time.sleep(1) 
        
        # --- COLOCAR STOP LOSS (TAKER / MERCADO) ---
        print(f"🛡️ Colocando Stop Loss en: {stop_loss:.2f}")
        try:
            exchange.create_order(
                symbol=simbolo, type='STOP_MARKET', side=lado_salida, 
                amount=cantidad, params={'positionSide': position_side, 'stopPrice': stop_loss}
            )
            print("✅ Stop Loss automatizado con éxito (Mercado).")
        except Exception as e:
            print(f"⚠️ Error al colocar SL: {e}")
            
        # --- COLOCAR TAKE PROFIT (MAKER / LIMIT) ---
        print(f"🎯 Colocando Take Profit LIMIT (1:{RATIO_RR}) en: {take_profit:.2f}")
        try:
            # Aquí cambiamos de TAKE_PROFIT_MARKET a LIMIT y pasamos el precio directamente
            exchange.create_order(
                symbol=simbolo, type='LIMIT', side=lado_salida, 
                amount=cantidad, price=take_profit, params={'positionSide': position_side}
            )
            print("✅ Take Profit LIMIT automatizado con éxito (Cero/Bajas Comisiones).")
        except Exception as e:
            print(f"⚠️ Error al colocar TP: {e}")
            
        return True
    except Exception as e:
        print(f"❌ Error crítico en el Exchange: {e}")
        return False

def cerrar_posiciones_emergencia(simbolo, hora_texto):
    try:
        print(f"🕒 {hora_texto} NY alcanzadas. Cancelando órdenes y cerrando posiciones...")
        exchange.cancel_all_orders(simbolo)
        posiciones = exchange.fetch_positions([simbolo])
        for pos in posiciones:
            cantidad = float(pos.get('contracts', 0))
            if cantidad > 0:
                lado = pos['side']
                lado_cierre = 'sell' if lado == 'long' else 'buy'
                position_side = 'LONG' if lado == 'long' else 'SHORT'
                print(f"🔄 Cerrando posición {position_side} de {cantidad} BTC...")
                exchange.create_order(
                    symbol=simbolo, type='MARKET', side=lado_cierre,
                    amount=cantidad, params={'positionSide': position_side}
                )
                print(f"✅ Cierre por fin de sesión ({hora_texto}) completado.")
    except Exception as e:
        print(f"❌ Error cerrando posición: {e}")

# ==========================================
# 3. BUCLE DEL BOT (VIGÍA EMAS)
# ==========================================
def iniciar_bot():
    print("🤖 BOT VIVO: EMAs (20/55) - Doble Sesión. Órdenes LIMIT. Riesgo: 3%. TP optimizado.")
    
    trade_abierto = False
    estado_espera = None
    crossover_sl_ref = None 
    tiempo_cruce = None             
    ultimo_cruce_procesado = None
    ultima_vela_evaluada = None   
    
    while True:
        try:
            ahora_ny = datetime.now(tz_ny)
            hora_actual = ahora_ny.time()
            dia_semana = ahora_ny.weekday() # 0 = Lunes, 6 = Domingo
            
            # --- 1. CIERRES DE EMERGENCIA ---
            es_hora_cierre_manana = (hora_actual.hour == 11 and hora_actual.minute == 0)
            es_hora_cierre_noche = (hora_actual.hour == 23 and hora_actual.minute == 0)
            
            if trade_abierto and (es_hora_cierre_manana or es_hora_cierre_noche):
                hora_str = "11:00" if es_hora_cierre_manana else "23:00"
                cerrar_posiciones_emergencia(SIMBOLO, hora_str)
                trade_abierto = False
                time.sleep(60) 
                continue
                
            # --- 2. VERIFICAR CIERRE NATURAL POR SL/TP ---
            # ¡OJO A ESTA SECCIÓN! Esto es lo que limpia el TP o SL que quede "colgando"
            if trade_abierto:
                try:
                    posiciones = exchange.fetch_positions([SIMBOLO])
                    en_posicion = any(float(p.get('contracts', 0)) > 0 for p in posiciones)
                    if not en_posicion:
                        print("♻️ Posición cerrada (SL o TP alcanzado). Limpiando órdenes huérfanas...")
                        exchange.cancel_all_orders(SIMBOLO) # Elimina SL o TP restante
                        trade_abierto = False
                except Exception:
                    pass

            # --- 3. DEFINIR VENTANAS OPERATIVAS ---
            es_lunes_a_viernes = dia_semana in [0, 1, 2, 3, 4]
            es_domingo_a_jueves = dia_semana in [6, 0, 1, 2, 3]
            
            en_manana = es_lunes_a_viernes and (pd.to_datetime('08:00').time() <= hora_actual <= pd.to_datetime('10:30').time())
            en_noche = es_domingo_a_jueves and (pd.to_datetime('20:00').time() <= hora_actual <= pd.to_datetime('22:30').time())
            
            es_hora_operativa = en_manana or en_noche
            
            # --- 4. BÚSQUEDA DE ENTRADAS ---
            if es_hora_operativa and not trade_abierto:
                df = obtener_velas(SIMBOLO, timeframe='1m') 
                
                if not df.empty:
                    vela_viva = df.iloc[-1]
                    minuto_actual = ahora_ny.minute
                    minuto_vela_viva = vela_viva.name.minute
                    
                    if minuto_actual == minuto_vela_viva:
                        vela_cerrada = df.iloc[-2]     
                        vela_anterior = df.iloc[-3]    
                        
                        if ultima_vela_evaluada != vela_cerrada.name:
                            ultima_vela_evaluada = vela_cerrada.name
                            
                            print(f"📊 [SCAN] {vela_cerrada.name.strftime('%H:%M')} | Precio Cierre: {vela_cerrada['Close']:.2f} | EMA20: {vela_cerrada['EMA20']:.2f} | EMA55: {vela_cerrada['EMA55']:.2f}")
                            
                            # A. LÓGICA DE ESPERA (Por Filtro de Color)
                            if estado_espera == "Esperando_Long":
                                if vela_cerrada.name > tiempo_cruce: 
                                    if vela_cerrada['Close'] >= vela_cerrada['Open']: # Es verde
                                        print(f"💥 [Paso 2] Confirmación Alcista (Vela Verde) tras espera.")
                                        entrada = vela_cerrada['Close']
                                        stop_loss = min(crossover_sl_ref, vela_cerrada['Low'])
                                        riesgo_precio = entrada - stop_loss
                                        take_profit = entrada + (riesgo_precio * RATIO_RR)
                                        
                                        if ejecutar_orden(SIMBOLO, 'Long', entrada, stop_loss, take_profit):
                                            trade_abierto = True
                                    else:
                                        print("❌ [Paso 2] Falló la confirmación Alcista (Salió Roja). Abortando.")
                                    estado_espera = None 
                                    
                            elif estado_espera == "Esperando_Short":
                                if vela_cerrada.name > tiempo_cruce:
                                    if vela_cerrada['Close'] <= vela_cerrada['Open']: # Es roja
                                        print(f"💥 [Paso 2] Confirmación Bajista (Vela Roja) tras espera.")
                                        entrada = vela_cerrada['Close']
                                        stop_loss = max(crossover_sl_ref, vela_cerrada['High'])
                                        riesgo_precio = stop_loss - entrada
                                        take_profit = entrada - (riesgo_precio * RATIO_RR)
                                        
                                        if ejecutar_orden(SIMBOLO, 'Short', entrada, stop_loss, take_profit):
                                            trade_abierto = True
                                    else:
                                        print("❌ [Paso 2] Falló la confirmación Bajista (Salió Verde). Abortando.")
                                    estado_espera = None

                            # B. BUSCAR NUEVOS CRUCES EN LA VELA RECIÉN CERRADA
                            elif estado_espera is None:
                                cruce_alcista = (vela_anterior['EMA20'] <= vela_anterior['EMA55']) and (vela_cerrada['EMA20'] > vela_cerrada['EMA55'])
                                
                                if cruce_alcista and vela_cerrada.name != ultimo_cruce_procesado:
                                    ultimo_cruce_procesado = vela_cerrada.name
                                    if vela_cerrada['Close'] >= vela_cerrada['Open']: 
                                        print(f"🚀 [Paso 1] Cruce Alcista Directo (Vela Verde). Ejecutando...")
                                        entrada = vela_cerrada['Close']
                                        stop_loss = vela_cerrada['Low']
                                        riesgo_precio = entrada - stop_loss
                                        take_profit = entrada + (riesgo_precio * RATIO_RR)
                                        
                                        if ejecutar_orden(SIMBOLO, 'Long', entrada, stop_loss, take_profit):
                                            trade_abierto = True
                                    else:
                                        print(f"👀 [Paso 1] Cruce Alcista, pero vela ROJA. Entrando en modo espera (Aguardando confirmación)...")
                                        estado_espera = "Esperando_Long"
                                        crossover_sl_ref = vela_cerrada['Low']
                                        tiempo_cruce = vela_cerrada.name 
                                
                                cruce_bajista = (vela_anterior['EMA20'] >= vela_anterior['EMA55']) and (vela_cerrada['EMA20'] < vela_cerrada['EMA55'])
                                
                                if cruce_bajista and vela_cerrada.name != ultimo_cruce_procesado:
                                    ultimo_cruce_procesado = vela_cerrada.name
                                    if vela_cerrada['Close'] <= vela_cerrada['Open']: 
                                        print(f"🚀 [Paso 1] Cruce Bajista Directo (Vela Roja). Ejecutando...")
                                        entrada = vela_cerrada['Close']
                                        stop_loss = vela_cerrada['High']
                                        riesgo_precio = stop_loss - entrada
                                        take_profit = entrada - (riesgo_precio * RATIO_RR)
                                        
                                        if ejecutar_orden(SIMBOLO, 'Short', entrada, stop_loss, take_profit):
                                            trade_abierto = True
                                    else: 
                                        print(f"👀 [Paso 1] Cruce Bajista, pero vela VERDE. Entrando en modo espera (Aguardando confirmación)...")
                                        estado_espera = "Esperando_Short"
                                        crossover_sl_ref = vela_cerrada['High']
                                        tiempo_cruce = vela_cerrada.name 

            # --- 5. GESTIÓN DE TIEMPO (SMART SLEEP) ---
            if not trade_abierto and es_hora_operativa:
                segundos_actuales = ahora_ny.second
                segundos_restantes = 60 - segundos_actuales
                time.sleep(segundos_restantes + 2)
            else:
                time.sleep(30) 
                
        except Exception as e:
            print(f"⚠️ Excepción en el ciclo principal: {e}")
            time.sleep(10)

if __name__ == "__main__":
    iniciar_bot()
