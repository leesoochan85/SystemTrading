import requests 
from bs4 import BeautifulSoup
import numpy as np
import pandas as pd
from datetime import datetime

BASE_URL = 'https://finance.naver.com/sise/sise_market_sum.naver?sosok='
START_PAGE = 1
fields = []
CODES =[0,1]

headers = {
    "User-Agent": "Mozilla/5.0"
}

now = datetime.now()
formattedDate = now.strftime("%Y%m%d")

def execute_crawler():
    df_total = []
    
    for code in CODES:

        res = requests.get(BASE_URL + str(code), headers=headers)
        page_soup = BeautifulSoup(res.text, 'lxml')

        total_page_num = page_soup.select_one('td.pgRR > a')
        total_page_num = int(total_page_num.get('href').split('=')[-1])

        ipt_html = page_soup.select_one('div.subcnt_sise_item_top')
        
        global fields 
        fields = [item.get('value') for item in ipt_html.select('input')]

        result = [crawler(code, str(page)) for page in range(1, total_page_num+1)]
        result = [df for df in result if not df.empty]
        
        if result:
            df = pd.concat(result, axis=0, ignore_index = True)
            df_total.append(df)
    

    df_total = pd.concat(df_total, ignore_index = True)
    df_total.reset_index(inplace=True, drop=True)
    df_total.to_excel('NaverFinance.xlsx')

    return df_total

def crawler(code, page):

    url = f"{BASE_URL}{code}&page={page}"
    res = requests.get(url, headers=headers)
    res.raise_for_status()
    page_soup = BeautifulSoup(res.text, 'lxml')

    table_html = page_soup.select_one("div.box_type_l table.type_2, div.box_type_1 table.type_2") # box_type_l은 코스피, box_type_1은 코스닥 
   
    if table_html is None:
        print(f"[WARN] table_html not found: code={code}, page={page}")
        print(page_soup.text[:500])
        return pd.DataFrame()
    
    header_data = [th.get_text(" ", strip = True) for  th in table_html.select("thead th")] [1:-1]
    rows = []
    for tr in table_html.select('tbody tr'):
        tds = tr.find_all('td')
        if not tds:
            continue

        no_td = tr.select_one('td.no')
        if no_td is None:
            continue

        data_tds = tds[1: 1 + len(header_data)]
        if len(data_tds) != len(header_data):
            continue

        row = []

        for td in data_tds:
            text = td.get_text(" ", strip=True)
            text = " ".join(text.split())
            row.append(text)

        rows.append(row)
        
    if not rows:
            return pd.DataFrame(columns=header_data)
        
    df = pd.DataFrame(rows, columns=header_data)
    return df
        
    # inner_data = [item.get_text().strip() for item in table_html.find_all(lambda x: (x.name == 'a' and 'title' in x.get('class',[])) or (x.name == 'td' and 'number' in x.get('class',[])))]
    # no_data = [item.get_text().strip() for item in table_html.select('td.no')]
    # number_data = np.array(inner_data)

    # number_data.resize(len(no_data), len(header_data))
    # df = pd.DataFrame(data=number_data, columns=header_data)
    # return df 

def get_universe():
    df = execute_crawler()

    # print("execute_crawler rows:", len(df))
    # print("columns:", df.columns.tolist())
    # print(df.head())

    mapping = {',': '', 'N/A': '0'}
    df.replace(mapping, regex=True, inplace=True)

    cols = ['거래량', 'ROE', 'PER']
    df[cols] = df[cols].astype(float)

    df = df[(df['거래량'] > 0) &
            (df['ROE'] > 0) & 
            (df['PER'] > 0) &
            (~df['종목명'].str.contains("지주",na=False)) &
            (~df['종목명'].str.contains("홀딩스",na=False))
            ]
    
    # print("filtered rows:", len(df))
    # print(df.head())

    df['1/PER'] = 1 / df['PER']
    df['RANK_ROE'] = df['ROE'].rank(method ='max', ascending=False)
    df['RANK_1/PER'] = df['1/PER'].rank(method ='max', ascending=False)
    df['RANK_VALUE'] = (df['RANK_ROE'] + df['RANK_1/PER'])/2

    df = df.sort_values(by=['RANK_VALUE'])
    df.reset_index(inplace=True, drop=True)
    df= df.loc[:199]

    df.to_excel('universe.xlsx')
    return df ['종목명'].tolist()

if __name__ == "__main__":
    print('Start!')
    get_universe()
    print('End')







# res = requests.get(BASE_URL + str(CODES[0]))
# page_soup = BeautifulSoup(res.text, 'lxml')
# # print(page_soup)

# total_page_num = page_soup.select_one('td.pgRR > a')
# print(total_page_num)
# total_page_num = int(total_page_num.get('href').split('=')[-1])
# print(total_page_num)

# ipt_html = page_soup.select_one('div.subcnt_sise_item_top')
# fields = [item.get('value')for item in ipt_html.select('input')]
# print(fields)