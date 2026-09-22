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
def obtener_velas(simbolo, timeframe='15m', limite=10):
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
        cantidad = RIESGO_USD / riesgo_precio
        cantidad = round(cantidad, 4) # BingX BTC usa max 4 decimales
        
        lado_entrada = 'buy' if tipo_trade == 'Long' else 'sell'
        lado_salida = 'sell' if tipo_trade == 'Long' else 'buy'
        position_side = 'LONG' if tipo_trade == 'Long' else 'SHORT'
        
        print(f"🧮 [DEBUG] Riesgo: ${RIESGO_USD} | Dist. SL: ${riesgo_precio:.2f} | Ordenando: {cantidad} BTC")
        print(f"🚀 Lanzando orden REAL {lado_entrada.upper()} en RETESTEO...")
        
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
        
        # 2. COLOCAR STOP LOSS (Corregido para Hedge Mode sin ReduceOnly)
        print(f"🛡️ Colocando Stop Loss en: {stop_loss}")
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
            
        # 3. COLOCAR TAKE PROFIT (Corregido para Hedge Mode sin ReduceOnly)
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

def cerrar_posiciones_1200(simbolo):
    try:
        print("🕒 12:00 NY alcanzadas. Cancelando órdenes y cerrando posiciones...")
        
        # 1. Cancelar órdenes de SL y TP
        exchange.cancel_all_orders(simbolo)
        print("✅ Órdenes pendientes canceladas.")
        
        # 2. Obtener la posición activa
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
                print("✅ Cierre por fin de sesión (12:00) completado con éxito.")
                
    except Exception as e:
        print(f"❌ Error cerrando posición: {e}")

# ==========================================
# 3. BUCLE DEL BOT (VIGÍA)
# ==========================================
def iniciar_bot():
    print("🤖 BOT ORB 15m - Pura Acción del Precio (En Vivo) Iniciado. Esperando...")
    
    trade_abierto_hoy = False
    estado_ruptura = None
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
                max_orb = None
                min_orb = None
                print(f"🔄 Reseteo diario de variables: {ahora_ny.date()}")
                time.sleep(60)
            
            # Si es fin de semana, dormir 1 hora
            if ahora_ny.weekday() >= 5:
                time.sleep(3600)
                continue
            
            # Si ya se operó, solo vigilar el cierre a las 12:00
            if trade_abierto_hoy:
                if hora_actual.hour == 12 and hora_actual.minute == 0:
                    cerrar_posiciones_1200(SIMBOLO)
                    time.sleep(60) # Pausa para evitar repeticiones en el mismo minuto
                time.sleep(30)
                continue

            # 1. ESTABLECER LA ORB DE 15 MIN (Leída a las 09:45 exactas)
            if hora_actual.hour == 9 and hora_actual.minute == 45 and max_orb is None:
                df = obtener_velas(SIMBOLO, timeframe='15m', limite=5)
                if not df.empty:
                    vela_orb = df.loc[df.index.time == pd.to_datetime('09:30').time()]
                    
                    if not vela_orb.empty:
                        max_orb = vela_orb['High'].iloc[0]
                        min_orb = vela_orb['Low'].iloc[0]
                        print(f"📦 ORB 15m (09:30-09:45) Definida | Techo: {max_orb} | Suelo: {min_orb}")
            
            # --- Condición de horario limitada hasta las 10:44:59 ---
            es_hora_operativa = (hora_actual.hour == 9 and hora_actual.minute >= 45) or (hora_actual.hour == 10 and hora_actual.minute < 45)
            
            # 2. BÚSQUEDA DE RUPTURA Y RETESTEO (09:45 a 10:45)
            if max_orb is not None and es_hora_operativa:
                df = obtener_velas(SIMBOLO, timeframe='15m', limite=10)
                
                if not df.empty:
                    vela_actual = df.iloc[-1]
                    vela_anterior = df.iloc[-2] # La última vela CERRADA
                    
                    # a) Verificar Ruptura (Pura acción del precio)
                    if estado_ruptura is None:
                        cierre_ant = vela_anterior['Close']
                        
                        if cierre_ant > max_orb:
                            estado_ruptura = 'Long'
                            print(f"👀 Ruptura ALCISTA confirmada. Esperando RETESTEO exacto en {max_orb}...")
                        elif cierre_ant < min_orb:
                            estado_ruptura = 'Short'
                            print(f"👀 Ruptura BAJISTA confirmada. Esperando RETESTEO exacto en {min_orb}...")
                    
                    # b) Cazar el Retesteo
                    elif estado_ruptura is not None:
                        precio_vivo = vela_actual['Low'] if estado_ruptura == 'Long' else vela_actual['High']
                        
                        if estado_ruptura == 'Long' and precio_vivo <= max_orb:
                            print(f"💥 ¡RETESTEO ALCISTA ALCANZADO! Precio en vivo tocó: {max_orb}")
                            take_profit = max_orb + (abs(max_orb - min_orb) * RATIO_RR)
                            exito = ejecutar_orden(SIMBOLO, 'Long', max_orb, min_orb, take_profit)
                            if exito: trade_abierto_hoy = True
                            
                        elif estado_ruptura == 'Short' and precio_vivo >= min_orb:
                            print(f"💥 ¡RETESTEO BAJISTA ALCANZADO! Precio en vivo tocó: {min_orb}")
                            take_profit = min_orb - (abs(max_orb - min_orb) * RATIO_RR)
                            exito = ejecutar_orden(SIMBOLO, 'Short', min_orb, max_orb, take_profit)
                            if exito: trade_abierto_hoy = True

            # Gestión inteligente de Rate Limits de la API
            if max_orb is not None and estado_ruptura is not None and es_hora_operativa:
                time.sleep(2) # Escaneo ultra-rápido si estamos cazando retesteo
            else:
                time.sleep(30) # Escaneo relajado fuera de la ventana de oportunidad o sin señales
                
        except Exception as e:
            print(f"⚠️ Excepción en el ciclo principal: {e}")
            time.sleep(10)

if __name__ == "__main__":
    iniciar_bot()