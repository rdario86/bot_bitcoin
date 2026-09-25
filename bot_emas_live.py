import ccxt
import pandas as pd
import time
from datetime import datetime
import pytz
import math 

# ==========================================
# 1. CONFIGURACIÓN DEL USUARIO
# ==========================================
API_KEY = 'WJkk2CHt59Gv3iphdXf03NozkFYlxwaCrNqFi9or9CxPWHTRNmUA72O3uCrXg98ZUCoNpXqw5gfJdZ1u7UQ'
API_SECRET = 'sOq1gjKraySQWcu34ZkW8SqoSCwZiWzR8I7VCXAZDmi88xVMLkQye6ZQiOsdEGvgt4KONydDXsFRujxlT1FTQ'

SIMBOLO = 'BTC/USDT:USDT'
APALANCAMIENTO = 25 
RATIO_RR = 2.0      # Ratio Beneficio (1:2)

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
def obtener_velas(simbolo, timeframe='1m', limite=1000):
    """
    🌟 CORRECCIÓN CRÍTICA: Se aumentó a 1000 velas. 
    Para que la EMA 55 sea idéntica a TradingView, necesita al menos 
    250-500 velas de datos previos para "suavizarse" matemáticamente.
    """
    try:
        velas = exchange.fetch_ohlcv(simbolo, timeframe=timeframe, limit=limite)
        df = pd.DataFrame(velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        df.index = df.index.tz_localize('UTC').tz_convert(tz_ny)
        
        # Calcular EMAs de forma idéntica a las plataformas de charting
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA55'] = df['Close'].ewm(span=55, adjust=False).mean()
        
        return df
    except Exception as e:
        print(f"Error descargando velas: {e}")
        return pd.DataFrame()

def ejecutar_orden(simbolo, tipo_trade, entrada, stop_loss, take_profit):
    try:
        try:
            exchange.set_leverage(APALANCAMIENTO, simbolo)
        except Exception:
            pass 
            
        balance_info = exchange.fetch_balance()
        balance_usdt = balance_info.get('USDT', {}).get('free', 0.0)
        
        if balance_usdt <= 0:
            print("❌ Error: No hay balance USDT disponible para operar.")
            return False

        balance_utilizable = balance_usdt * 0.95
        riesgo_precio = abs(entrada - stop_loss)
        
        if riesgo_precio == 0: 
            margen = entrada * 0.0005
            riesgo_precio = margen
            stop_loss = (entrada - margen) if tipo_trade == 'Long' else (entrada + margen)
        
        sl_porcentaje = (riesgo_precio / entrada) * 100
        riesgo_cuenta_pct = sl_porcentaje * APALANCAMIENTO
        monto_a_arriesgar = balance_utilizable * (riesgo_cuenta_pct / 100)
        
        cantidad_bruta = monto_a_arriesgar / riesgo_precio
        cantidad = math.floor(cantidad_bruta * 10000) / 10000 
        
        if cantidad <= 0:
            print("❌ Error: Capital insuficiente para comprar el mínimo permitido de BTC.")
            return False
            
        lado_entrada = 'buy' if tipo_trade == 'Long' else 'sell'
        lado_salida = 'sell' if tipo_trade == 'Long' else 'buy'
        position_side = 'LONG' if tipo_trade == 'Long' else 'SHORT'
        
        print(f"💰 [CAPITAL] Balance: ${balance_utilizable:.2f} USDT | Riesgo: {riesgo_cuenta_pct:.2f}%")
        print(f"🚀 Lanzando orden REAL {lado_entrada.upper()} por {cantidad} BTC...")
        
        orden = exchange.create_order(
            symbol=simbolo, type='MARKET', side=lado_entrada, 
            amount=cantidad, params={'positionSide': position_side}
        )
        print(f"✅ ¡Posición abierta! ID: {orden.get('id')}")
        
        time.sleep(2) 
        
        print(f"🛡️ Colocando Stop Loss en: {stop_loss:.2f}")
        try:
            exchange.create_order(
                symbol=simbolo, type='STOP_MARKET', side=lado_salida, 
                amount=cantidad, params={'positionSide': position_side, 'stopPrice': stop_loss}
            )
            print("✅ Stop Loss automatizado con éxito.")
        except Exception as e:
            print(f"⚠️ Error al colocar SL: {e}")
            
        print(f"🎯 Colocando Take Profit (1:{RATIO_RR}) en: {take_profit:.2f}")
        try:
            exchange.create_order(
                symbol=simbolo, type='TAKE_PROFIT_MARKET', side=lado_salida, 
                amount=cantidad, params={'positionSide': position_side, 'stopPrice': take_profit}
            )
            print("✅ Take Profit automatizado con éxito.")
        except Exception as e:
            print(f"⚠️ Error al colocar TP: {e}")
            
        return True
    except Exception as e:
        print(f"❌ Error crítico en el Exchange: {e}")
        return False

def cerrar_posiciones_emergencia(simbolo, hora_texto="11:00"):
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
                print(f"✅ Cierre por fin de sesión completado.")
    except Exception as e:
        print(f"❌ Error cerrando posición: {e}")

# ==========================================
# 3. BUCLE DEL BOT (VIGÍA EMAS)
# ==========================================
def iniciar_bot():
    print("🤖 BOT VIVO: EMAs (20/55) con Filtro de Color. Escaneando...")
    
    trade_abierto = False
    estado_espera = None
    crossover_sl_ref = None 
    tiempo_cruce = None             # 🌟 CORRECCIÓN: Memorizar hora exacta del cruce
    ultimo_cruce_procesado = None   # 🌟 CORRECCIÓN: Prevenir doble ejecución del mismo cruce
    
    while True:
        try:
            ahora_ny = datetime.now(tz_ny)
            hora_actual = ahora_ny.time()
            
            if ahora_ny.weekday() >= 5:
                time.sleep(3600)
                continue
            
            # Cierre Forzado 11:00 NY
            if trade_abierto and hora_actual.hour == 11 and hora_actual.minute == 0:
                cerrar_posiciones_emergencia(SIMBOLO, "11:00")
                trade_abierto = False
                time.sleep(60)
                continue
                
            if trade_abierto:
                try:
                    posiciones = exchange.fetch_positions([SIMBOLO])
                    en_posicion = any(float(p.get('contracts', 0)) > 0 for p in posiciones)
                    if not en_posicion:
                        print("♻️ Posición cerrada (SL/TP alcanzado). Listo para buscar nuevos cruces.")
                        exchange.cancel_all_orders(SIMBOLO)
                        trade_abierto = False
                except Exception:
                    pass

            # Ventana Operativa: 08:00 a 10:30 NY
            es_hora_operativa = (hora_actual >= pd.to_datetime('08:00').time()) and (hora_actual <= pd.to_datetime('10:30').time())
            
            if es_hora_operativa and not trade_abierto:
                # 🌟 SE UTILIZA EL LÍMITE DE 1000 VELAS AHORA
                df = obtener_velas(SIMBOLO, timeframe='1m', limite=1000)
                
                if not df.empty:
                    vela_viva = df.iloc[-1]
                    minuto_actual = ahora_ny.minute
                    minuto_vela_viva = vela_viva.name.minute
                    
                    if minuto_actual == minuto_vela_viva:
                        vela_cerrada = df.iloc[-2]     
                        vela_anterior = df.iloc[-3]    
                        
                        # A. LÓGICA DE ESPERA (Por Filtro de Color)
                        if estado_espera == "Esperando_Long":
                            # 🌟 CORRECCIÓN: Solo evaluar si ya se formó una vela NUEVA (mayor a la del cruce)
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
                            # Si vela_cerrada.name == tiempo_cruce, seguimos en el mismo minuto, solo esperamos.
                            
                        elif estado_espera == "Esperando_Short":
                            # 🌟 CORRECCIÓN: Solo evaluar si ya se formó una vela NUEVA
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
                            # 🌟 CORRECCIÓN: Evitar procesar el mismo cruce repetidas veces
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

            if not trade_abierto and es_hora_operativa:
                time.sleep(10)
            else:
                time.sleep(30)
                
        except Exception as e:
            print(f"⚠️ Excepción en el ciclo principal: {e}")
            time.sleep(10)

if __name__ == "__main__":
    iniciar_bot()
