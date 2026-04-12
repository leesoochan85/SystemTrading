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

    table_html = page_soup.select_one("div.box_type_l table.type_2, div.box_type_1 table.type_2")
    if table_html is None:
        print(f"[WARN] table_html not found: code={code}, page={page}")
        return pd.DataFrame()

    header_data = ["종목코드"] + [th.get_text(" ", strip=True) for th in table_html.select("thead th")][1:-1]

    rows = []
    for tr in table_html.select("tbody tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        no_td = tr.select_one("td.no")
        if no_td is None:
            continue

        name_a = tr.select_one("a.tltle")
        if name_a is None:
            continue

        href = name_a.get("href", "")
        if "code=" not in href:
            continue

        stock_code = href.split("code=")[-1].split("&")[0]

        data_tds = tds[1:1 + (len(header_data) - 1)]
        if len(data_tds) != len(header_data) - 1:
            continue

        row = [stock_code]
        for td in data_tds:
            text = td.get_text(" ", strip=True)
            text = " ".join(text.split())
            row.append(text)

        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=header_data)

    return pd.DataFrame(rows, columns=header_data)

def get_universe(return_df=False):
    df = execute_crawler()

    mapping = {',': '', 'N/A': '0'}
    df.replace(mapping, regex=True, inplace=True)

    cols = ['거래량', 'ROE', 'PER']
    df[cols] = df[cols].astype(float)

    df = df[(df['거래량'] > 0) &
            (df['ROE'] > 0) &
            (df['PER'] > 0) &
            (~df['종목명'].str.contains("지주", na=False)) &
            (~df['종목명'].str.contains("홀딩스", na=False))]

    df['1/PER'] = 1 / df['PER']
    df['RANK_ROE'] = df['ROE'].rank(method='max', ascending=False)
    df['RANK_1/PER'] = df['1/PER'].rank(method='max', ascending=False)
    df['RANK_VALUE'] = (df['RANK_ROE'] + df['RANK_1/PER']) / 2
    df["종목코드"] = df["종목코드"].astype(str).str.strip()
    df = df[df["종목코드"].str.fullmatch(r"\d{6}", na=False)]

    df = df.sort_values(by=['RANK_VALUE']).reset_index(drop=True)
    df = df.loc[:199, ['종목코드', '종목명', '현재가', '거래량', 'PER', 'ROE']]

    df.to_excel('universe.xlsx', index=False)

    if return_df:
        return df

    return df['종목명'].tolist()

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