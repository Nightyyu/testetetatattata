from flask import Flask, jsonify, request
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import sqlite3
import logging
import os
import re

app = Flask(__name__)

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Inicialização do banco SQLite
def init_db():
    with sqlite3.connect('stock_data.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                stock INTEGER,
                price INTEGER,
                last_updated TEXT
            )
        ''')
        conn.commit()

# Função para salvar dados no banco
def save_to_db(category, items, last_updated):
    with sqlite3.connect('stock_data.db') as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM stock WHERE category = ?', (category,))
        for item in items:
            cursor.execute('''
                INSERT INTO stock (category, name, stock, price, last_updated)
                VALUES (?, ?, ?, ?, ?)
            ''', (category, item['name'], item['stock'], item.get('price', 0), last_updated))
        conn.commit()

# Função para carregar dados do banco
def load_from_db(category=None):
    with sqlite3.connect('stock_data.db') as conn:
        cursor = conn.cursor()
        if category:
            cursor.execute('SELECT name, stock, price, last_updated FROM stock WHERE category = ?', (category,))
            rows = cursor.fetchall()
            return [{'name': row[0], 'stock': row[1], 'price': row[2]} for row in rows], rows[0][3] if rows else None
        else:
            data = {'seeds': [], 'gear': [], 'egg_shop': [], 'honey': [], 'cosmetics': [], 'last_updated': None}
            for cat in ['seeds', 'gear', 'egg_shop', 'honey', 'cosmetics']:
                cursor.execute('SELECT name, stock, price, last_updated FROM stock WHERE category = ?', (cat,))
                rows = cursor.fetchall()
                data[cat] = [{'name': row[0], 'stock': row[1], 'price': row[2]} for row in rows]
                if rows and not data['last_updated']:
                    data['last_updated'] = rows[0][3]
            return data

def parse_update_time(time_text):
    """Converte texto como '03m 56s' ou '01h 13m 56s' em segundos."""
    time_text = time_text.lower().strip()
    
    # Regex para capturar horas, minutos e segundos
    pattern = r'(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?'
    match = re.search(pattern, time_text)
    
    if not match:
        return 300  # 5 minutos como padrão
    
    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2)) if match.group(2) else 0
    seconds = int(match.group(3)) if match.group(3) else 0
    
    total_seconds = hours * 3600 + minutes * 60 + seconds
    return max(total_seconds, 30)  # Mínimo de 30 segundos

def scrape_stock():
    """Raspa os dados de estoque do site."""
    url = 'https://vulcanvalues.com/grow-a-garden/stock'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    last_updated = datetime.now().isoformat()
    next_update_times = {}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        logger.info(f"Status da resposta: {response.status_code}")
        logger.info(f"Tamanho do HTML: {len(response.text)} caracteres")

        new_data = {
            'seeds': [],
            'gear': [],
            'egg_shop': [],
            'honey': [],
            'cosmetics': []
        }

        # Debug: Vamos ver o que tem na página
        logger.info("Procurando por elementos principais...")
        
        # Tentar encontrar diferentes estruturas
        possible_containers = [
            'div.grid',
            'div[class*="grid"]',
            'div[class*="stock"]',
            'div[class*="container"]',
            'main',
            'section'
        ]
        
        for selector in possible_containers:
            elements = soup.select(selector)
            if elements:
                logger.info(f"Encontrados {len(elements)} elementos com seletor: {selector}")
                
        # Vamos procurar por todos os h2 para ver as categorias
        all_h2 = soup.find_all('h2')
        logger.info(f"Encontrados {len(all_h2)} elementos h2")
        for h2 in all_h2:
            logger.info(f"H2 encontrado: {h2.text.strip()}")

        # Encontrar a seção de estoques
        stock_grid = soup.find('div', class_='grid grid-cols-1 md:grid-cols-3 gap-6 px-6 text-left max-w-screen-lg mx-auto')
        if not stock_grid:
            logger.error("Seção de estoque não encontrada - tentando alternativas...")
            # Tentar outras estruturas
            stock_grid = soup.find('div', class_='grid')
            if not stock_grid:
                stock_grid = soup.find('main') or soup.find('section')
            
        if not stock_grid:
            logger.error("Nenhuma estrutura principal encontrada")
            return

        logger.info("Estrutura principal encontrada, procurando categorias...")

        # Iterar pelas seções de cada categoria
        sections_found = 0
        for section in stock_grid.find_all('div'):
            h2 = section.find('h2')
            if not h2:
                continue
                
            sections_found += 1
            category = h2.text.strip().lower()
            logger.info(f"Processando categoria: {category}")
            
            # Procurar pelo tempo de atualização
            update_time_text = ""
            update_paragraph = section.find('p', string=re.compile(r'UPDATES IN:', re.IGNORECASE))
            if not update_paragraph:
                # Tentar encontrar em qualquer texto que contenha "UPDATES IN"
                for p in section.find_all(['p', 'div', 'span']):
                    if p.get_text() and 'updates in:' in p.get_text().lower():
                        update_time_text = p.get_text()
                        break
            else:
                update_time_text = update_paragraph.get_text()
            
            if update_time_text:
                # Extrair o tempo (ex: "UPDATES IN: 03m 56s")
                time_match = re.search(r'updates in:\s*(.+)', update_time_text.lower())
                if time_match:
                    time_str = time_match.group(1).strip()
                    update_seconds = parse_update_time(time_str)
                    logger.info(f"Categoria {category}: próxima atualização em {update_seconds}s")
                else:
                    update_seconds = 300  # 5 minutos padrão
            else:
                update_seconds = 300  # 5 minutos padrão
            
            if 'gear' in category:
                category_key = 'gear'
            elif 'egg' in category:
                category_key = 'egg_shop'
            elif 'seeds' in category:
                category_key = 'seeds'
            elif 'honey' in category:
                category_key = 'honey'
            elif 'cosmetics' in category:
                category_key = 'cosmetics'
            else:
                logger.info(f"Categoria não reconhecida: {category}")
                continue
            
            next_update_times[category_key] = update_seconds

            # Procurar pela lista de itens - vamos tentar diferentes estruturas
            ul = section.find('ul')
            if not ul:
                logger.warning(f"Lista não encontrada para categoria: {category}")
                # Vamos procurar por qualquer lista ou parágrafo com itens
                items_text = section.get_text()
                logger.info(f"Texto da seção {category}: {items_text[:200]}...")
                continue

            items_found = 0
            # Procurar por todos os itens na lista
            for li in ul.find_all('li'):
                # Extrair texto do item
                item_text = li.get_text().strip()
                logger.info(f"Item encontrado: {item_text}")
                
                if not item_text:
                    continue
                
                # Tentar extrair nome e quantidade do texto
                # Formato esperado: "Nome do Item x123" ou "Nome do Item"
                if ' x' in item_text:
                    parts = item_text.rsplit(' x', 1)
                    name = parts[0].strip()
                    try:
                        stock = int(parts[1].strip())
                    except (ValueError, IndexError):
                        stock = 0
                else:
                    name = item_text.strip()
                    stock = 1  # Se não tem quantidade, assumir 1
                
                if name:  # Se conseguiu extrair um nome válido
                    new_data[category_key].append({
                        'name': name,
                        'stock': stock,
                        'price': 0
                    })
                    items_found += 1

            logger.info(f"Categoria {category_key}: {items_found} itens encontrados")

        logger.info(f"Total de seções processadas: {sections_found}")
        
        # Log do total de itens por categoria
        total_items = 0
        for category, items in new_data.items():
            logger.info(f"{category}: {len(items)} itens")
            total_items += len(items)
        
        logger.info(f"Total de itens coletados: {total_items}")

        # Salvar no banco
        for category, items in new_data.items():
            save_to_db(category, items, last_updated)

        logger.info(f"Dados salvos no banco: {last_updated}")
        
        # Reagendar baseado no menor tempo de atualização
        if next_update_times:
            min_seconds = min(next_update_times.values())
            min_category = min(next_update_times, key=next_update_times.get)
            
            logger.info(f"Próxima atualização em {min_seconds}s (categoria: {min_category})")
            logger.info(f"Tempos por categoria: {next_update_times}")
            
            # Reagendar o job
            try:
                scheduler.remove_job('stock_scraper')
            except:
                pass
            
            scheduler.add_job(
                scrape_stock, 
                'date', 
                run_date=datetime.now() + timedelta(seconds=min_seconds),
                id='stock_scraper'
            )
        else:
            # Se não conseguiu detectar tempos, usar padrão de 5 minutos
            logger.warning("Não foi possível detectar tempos de atualização, usando 5 minutos")
            try:
                scheduler.remove_job('stock_scraper')
            except:
                pass
            
            scheduler.add_job(
                scrape_stock, 
                'date', 
                run_date=datetime.now() + timedelta(minutes=5),
                id='stock_scraper'
            )
        
    except requests.RequestException as e:
        logger.error(f"Erro ao raspar: {str(e)}")
        # Em caso de erro, tentar novamente em 2 minutos
        try:
            scheduler.remove_job('stock_scraper')
        except:
            pass
        scheduler.add_job(
            scrape_stock, 
            'date', 
            run_date=datetime.now() + timedelta(minutes=2),
            id='stock_scraper'
        )
    except Exception as e:
        logger.error(f"Erro inesperado: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        # Em caso de erro, tentar novamente em 2 minutos
        try:
            scheduler.remove_job('stock_scraper')
        except:
            pass
        scheduler.add_job(
            scrape_stock, 
            'date', 
            run_date=datetime.now() + timedelta(minutes=2),
            id='stock_scraper'
        )

# Configuração do agendador
scheduler = BackgroundScheduler()
scheduler.start()

# Inicializa o banco e faz o scraping inicial
init_db()
scrape_stock()

@app.route('/')
def home():
    """Página inicial com informações sobre a API."""
    return jsonify({
        'message': 'API de Estoque Grow a Garden',
        'endpoints': {
            '/api/grow-a-garden/stock': 'GET - Obter dados de estoque',
            '/api/grow-a-garden/stock?category=CATEGORIA': 'GET - Obter dados de uma categoria específica',
            '/api/grow-a-garden/stock/refresh': 'GET - Forçar atualização dos dados'
        },
        'categorias_disponíveis': ['seeds', 'gear', 'egg_shop', 'honey', 'cosmetics']
    })

@app.route('/api/grow-a-garden/stock', methods=['GET'])
def get_stock():
    """Retorna os dados de estoque."""
    category = request.args.get('category')
    if category:
        items, last_updated = load_from_db(category)
        if not items:
            return jsonify({'error': 'Categoria não encontrada ou sem dados'}), 404
        return jsonify({category: items, 'last_updated': last_updated})
    return jsonify(load_from_db())

@app.route('/api/grow-a-garden/stock/refresh', methods=['GET'])
def refresh_stock():
    """Força a atualização dos dados."""
    scrape_stock()
    return jsonify({'message': 'Dados atualizados', 'last_updated': load_from_db()['last_updated']})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)