import ccxt
import pandas as pd
import time
from datetime import datetime
import pytz

# ==========================================
# 1. CONFIGURACIÓN DEL USUARIO
# ==========================================
API_KEY = 'WJkk2CHt59Gv3iphdXf03NozkFYlxwaCrNqFi9or9CxPWHTRNmUA72O3uCrXg98ZUCoNpXqw5gfJdZ1u7UQ'
API_SECRET = 'sOq1gjKraySQWcu34ZkW8SqoSCwZiWzR8I7VCXAZDmi88xVMLkQye6ZQiOsdEGvgt4KONydDXsFRujxlT1FTQ'

SIMBOLO = 'BTC/USDT:USDT'
RIESGO_USD = 1.0  # Riesgo por operación en dólares
RATIO_RR = 2.0    # Ratio Beneficio (1:2)

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
def obtener_velas(simbolo, timeframe='1m', limite=15):
    try:
        velas = exchange.fetch_ohlcv(simbolo, timeframe=timeframe, limit=limite)
        df = pd.DataFrame(velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        df.index = df.index.tz_localize('UTC').tz_convert(tz_ny)
        return df
    except Exception as e:
        print(f"Error descargando velas: {e}")
        return pd.DataFrame()

def ejecutar_orden(simbolo, tipo_trade, entrada, stop_loss, take_profit):
    try:
        riesgo_precio = abs(entrada - stop_loss)
        if riesgo_precio == 0: riesgo_precio = 0.01 # Prevención matemática
        
        cantidad = RIESGO_USD / riesgo_precio
        cantidad = round(cantidad, 4) # BingX BTC usa max 4 decimales
        
        lado_entrada = 'buy' if tipo_trade == 'Long' else 'sell'
        lado_salida = 'sell' if tipo_trade == 'Long' else 'buy'
        position_side = 'LONG' if tipo_trade == 'Long' else 'SHORT'
        
        print(f"🧮 [DEBUG] Riesgo: ${RIESGO_USD} | Dist. SL: ${riesgo_precio:.2f} | Ordenando: {cantidad} BTC")
        print(f"🚀 Lanzando orden REAL {lado_entrada.upper()} en CONFIRMACIÓN DE 1M...")
        
        # 1. ORDEN MARKET DE ENTRADA
        orden = exchange.create_order(
            symbol=simbolo, 
            type='MARKET', 
            side=lado_entrada, 
            amount=cantidad, 
            params={'positionSide': position_side}
        )
        print(f"✅ ¡Posición abierta! ID: {orden.get('id')}")
        
        time.sleep(2) # Pausa para que BingX procese la orden principal
        
        # 2. COLOCAR STOP LOSS (Adaptado a Hedge Mode)
        print(f"🛡️ Colocando Stop Loss estructural en: {stop_loss}")
        try:
            exchange.create_order(
                symbol=simbolo, 
                type='STOP_MARKET', 
                side=lado_salida, 
                amount=cantidad, 
                params={'positionSide': position_side, 'stopPrice': stop_loss}
            )
            print("✅ Stop Loss automatizado con éxito.")
        except Exception as e:
            print(f"⚠️ Error al colocar SL automático: {e}")
            
        # 3. COLOCAR TAKE PROFIT (Adaptado a Hedge Mode)
        print(f"🎯 Colocando Take Profit en: {take_profit}")
        try:
            exchange.create_order(
                symbol=simbolo, 
                type='TAKE_PROFIT_MARKET', 
                side=lado_salida, 
                amount=cantidad, 
                params={'positionSide': position_side, 'stopPrice': take_profit}
            )
            print("✅ Take Profit automatizado con éxito.")
        except Exception as e:
            print(f"⚠️ Error al colocar TP automático: {e}")
            
        return True
    except Exception as e:
        print(f"❌ Error en la ejecución de la orden: {e}")
        return False

def cerrar_posiciones_emergencia(simbolo, hora_texto="11:00"):
    try:
        print(f"🕒 {hora_texto} NY alcanzadas. Cancelando órdenes y cerrando posiciones...")
        
        exchange.cancel_all_orders(simbolo)
        print("✅ Órdenes pendientes canceladas.")
        
        posiciones = exchange.fetch_positions([simbolo])
        
        for pos in posiciones:
            cantidad = float(pos.get('contracts', 0))
            
            if cantidad > 0:
                lado = pos['side']
                lado_cierre = 'sell' if lado == 'long' else 'buy'
                position_side = 'LONG' if lado == 'long' else 'SHORT'
                
                print(f"🔄 Cerrando posición {position_side} de {cantidad} BTC...")
                exchange.create_order(
                    symbol=simbolo,
                    type='MARKET',
                    side=lado_cierre,
                    amount=cantidad,
                    params={'positionSide': position_side}
                )
                print(f"✅ Cierre por fin de sesión ({hora_texto}) completado con éxito.")
                
    except Exception as e:
        print(f"❌ Error cerrando posición: {e}")

# ==========================================
# 3. BUCLE DEL BOT (VIGÍA)
# ==========================================
def iniciar_bot():
    print("🤖 BOT ORB (1m Confirmación) - Pura Acción del Precio (En Vivo) Iniciado. Esperando...")
    
    trade_abierto_hoy = False
    estado_ruptura = None
    vela_ruptura_extremo = 0
    pullback_hecho = False
    max_orb = None
    min_orb = None
    
    while True:
        try:
            ahora_ny = datetime.now(tz_ny)
            hora_actual = ahora_ny.time()
            
            # Reseteo diario a medianoche
            if hora_actual.hour == 0 and hora_actual.minute == 0:
                trade_abierto_hoy = False
                estado_ruptura = None
                vela_ruptura_extremo = 0
                pullback_hecho = False
                max_orb = None
                min_orb = None
                print(f"🔄 Reseteo diario de variables: {ahora_ny.date()}")
                time.sleep(60)
            
            # Si es fin de semana, dormir
            if ahora_ny.weekday() >= 5:
                time.sleep(3600)
                continue
            
            # Cierre de emergencia a las 11:00 AM
            if trade_abierto_hoy and hora_actual.hour == 11 and hora_actual.minute == 0:
                cerrar_posiciones_emergencia(SIMBOLO, "11:00")
                time.sleep(60)
                continue

            # 1. ESTABLECER LA ORB DE LOS PRIMEROS 5 MINUTOS (A las 09:35 AM)
            if hora_actual.hour == 9 and hora_actual.minute == 35 and max_orb is None:
                df = obtener_velas(SIMBOLO, timeframe='1m', limite=10)
                if not df.empty:
                    # Filtramos exactamente los minutos 30, 31, 32, 33 y 34
                    hora_inicio = pd.to_datetime('09:30').time()
                    hora_fin = pd.to_datetime('09:34').time()
                    velas_orb = df.loc[(df.index.time >= hora_inicio) & (df.index.time <= hora_fin)]
                    
                    if len(velas_orb) == 5:
                        max_orb = velas_orb['High'].max()
                        min_orb = velas_orb['Low'].min()
                        print(f"📦 ORB 5m (09:30-09:34) Definida | Techo: {max_orb} | Suelo: {min_orb}")
            
            # Ventana operativa: Desde las 09:35 hasta las 10:29:59
            es_hora_operativa = (hora_actual.hour == 9 and hora_actual.minute >= 35) or (hora_actual.hour == 10 and hora_actual.minute < 30)
            
            # 2. SECUENCIA DE RUPTURA, PULLBACK Y CONFIRMACIÓN
            if max_orb is not None and es_hora_operativa and not trade_abierto_hoy:
                df = obtener_velas(SIMBOLO, timeframe='1m', limite=5)
                
                if not df.empty:
                    vela_actual = df.iloc[-1] # En formación
                    vela_anterior = df.iloc[-2] # Última CERRADA
                    
                    # FASE 1: RUPTURA INICIAL
                    if estado_ruptura is None:
                        cierre_ant = vela_anterior['Close']
                        
                        if cierre_ant > max_orb:
                            estado_ruptura = 'Long'
                            vela_ruptura_extremo = vela_anterior['Low'] # Mínimo de la vela que rompe
                            print(f"👀 [Paso 1] Ruptura ALCISTA. Stop Loss estructural fijado en: {vela_ruptura_extremo}. Esperando Pullback...")
                            
                        elif cierre_ant < min_orb:
                            estado_ruptura = 'Short'
                            vela_ruptura_extremo = vela_anterior['High'] # Máximo de la vela que rompe
                            print(f"👀 [Paso 1] Ruptura BAJISTA. Stop Loss estructural fijado en: {vela_ruptura_extremo}. Esperando Pullback...")
                    
                    # FASE 2: PULLBACK
                    elif estado_ruptura is not None and not pullback_hecho:
                        # Verificamos si en la vela en vivo, o en la última cerrada, el precio volvió a tocar la zona
                        if estado_ruptura == 'Long':
                            if vela_actual['Low'] <= max_orb or vela_anterior['Low'] <= max_orb:
                                pullback_hecho = True
                                print(f"🔥 [Paso 2] ¡Pullback detectado! Precio rozó el Techo ({max_orb}). Esperando cierre de confirmación de 1m...")
                                
                        elif estado_ruptura == 'Short':
                            if vela_actual['High'] >= min_orb or vela_anterior['High'] >= min_orb:
                                pullback_hecho = True
                                print(f"🔥 [Paso 2] ¡Pullback detectado! Precio rozó el Suelo ({min_orb}). Esperando cierre de confirmación de 1m...")
                    
                    # FASE 3: CONFIRMACIÓN Y GATILLO
                    elif pullback_hecho:
                        # Para confirmar, exigimos que la última vela CERRADA vuelva a quedar por fuera del rango
                        if estado_ruptura == 'Long':
                            if vela_anterior['Close'] > max_orb:
                                print(f"💥 [Paso 3] ¡Confirmación Alcista! Vela de 1m cerró por fuera del rango.")
                                precio_entrada = vela_anterior['Close']
                                take_profit = precio_entrada + (abs(precio_entrada - vela_ruptura_extremo) * RATIO_RR)
                                exito = ejecutar_orden(SIMBOLO, 'Long', precio_entrada, vela_ruptura_extremo, take_profit)
                                if exito: trade_abierto_hoy = True
                                
                        elif estado_ruptura == 'Short':
                            if vela_anterior['Close'] < min_orb:
                                print(f"💥 [Paso 3] ¡Confirmación Bajista! Vela de 1m cerró por debajo del rango.")
                                precio_entrada = vela_anterior['Close']
                                take_profit = precio_entrada - (abs(precio_entrada - vela_ruptura_extremo) * RATIO_RR)
                                exito = ejecutar_orden(SIMBOLO, 'Short', precio_entrada, vela_ruptura_extremo, take_profit)
                                if exito: trade_abierto_hoy = True

            # Gestión de llamadas a la API
            if max_orb is not None and estado_ruptura is not None and not trade_abierto_hoy and es_hora_operativa:
                time.sleep(2) # Escaneo ultra-rápido durante la cacería de Pullback y Confirmación
            else:
                time.sleep(20) # Escaneo relajado fuera de peligro
                
        except Exception as e:
            print(f"⚠️ Excepción en el ciclo principal: {e}")
            time.sleep(10)

if __name__ == "__main__":
    iniciar_bot()
