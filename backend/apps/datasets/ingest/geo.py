"""Small lookup lists for geo semantic types (country / city). Lower-cased, accent-free."""

from __future__ import annotations


def _split(text: str) -> frozenset[str]:
    return frozenset(item.strip() for item in text.replace("\n", ",").split(",") if item.strip())


COUNTRY_NAMES = _split(
    """
afghanistan, albania, algeria, andorra, angola, argentina, armenia, australia, austria, azerbaijan, bahamas,
bahrain, bangladesh, barbados, belarus, belgium, belize, benin, bhutan, bolivia, bosnia and herzegovina,
botswana, brazil, brunei, bulgaria, burkina faso, burundi, cambodia, cameroon, canada, cape verde, chad, chile,
china, colombia, comoros, congo, costa rica, croatia, cuba, cyprus, czechia, czech republic, denmark, djibouti,
dominica, dominican republic, ecuador, egypt, el salvador, eritrea, estonia, eswatini, ethiopia, fiji, finland,
france, gabon, gambia, georgia, germany, ghana, greece, grenada, guatemala, guinea, guyana, haiti, honduras,
hong kong, hungary, iceland, india, indonesia, iran, iraq, ireland, israel, italy, ivory coast, jamaica, japan,
jordan, kazakhstan, kenya, kiribati, kuwait, kyrgyzstan, laos, latvia, lebanon, lesotho, liberia, libya,
liechtenstein, lithuania, luxembourg, madagascar, malawi, malaysia, maldives, mali, malta, mauritania, mauritius,
mexico, moldova, monaco, mongolia, montenegro, morocco, mozambique, myanmar, namibia, nepal, netherlands,
new zealand, nicaragua, niger, nigeria, north korea, north macedonia, norway, oman, pakistan, palestine, panama,
papua new guinea, paraguay, peru, philippines, poland, portugal, qatar, romania, russia, russian federation,
rwanda, saudi arabia, senegal, serbia, seychelles, sierra leone, singapore, slovakia, slovenia, somalia,
south africa, south korea, korea, south sudan, spain, sri lanka, sudan, suriname, sweden, switzerland, syria,
taiwan, tajikistan, tanzania, thailand, togo, tonga, trinidad and tobago, tunisia, turkey, turkmenistan, uganda,
ukraine, united arab emirates, uae, united kingdom, uk, great britain, england, scotland, wales, united states,
united states of america, usa, us, uruguay, uzbekistan, vanuatu, venezuela, vietnam, yemen, zambia, zimbabwe
"""
)

# ISO 3166-1 alpha-2/alpha-3 codes (only trusted when the column name says "country").
COUNTRY_CODES = _split(
    """
in, ind, us, usa, gb, gbr, uk, ae, are, sg, sgp, au, aus, ca, can, de, deu, fr, fra, jp, jpn, cn, chn, br, bra,
za, zaf, nl, nld, it, ita, es, esp, ch, che, se, swe, no, nor, dk, dnk, fi, fin, ie, irl, nz, nzl, my, mys, th,
tha, id, idn, ph, phl, vn, vnm, kr, kor, sa, sau, qa, qat, kw, kwt, om, omn, bh, bhr, np, npl, lk, lka, bd, bgd,
pk, pak, mx, mex, ar, arg, cl, chl, co, col, pe, per, eg, egy, ng, nga, ke, ken, ru, rus, tr, tur, pl, pol, pt,
prt, be, bel, at, aut, gr, grc, il, isr, hk, hkg, tw, twn
"""
)

CITY_NAMES = _split(
    """
mumbai, delhi, new delhi, bengaluru, bangalore, hyderabad, ahmedabad, chennai, kolkata, surat, pune, jaipur,
lucknow, kanpur, nagpur, indore, thane, bhopal, visakhapatnam, vizag, pimpri chinchwad, patna, vadodara, baroda,
ghaziabad, ludhiana, agra, nashik, faridabad, meerut, rajkot, varanasi, srinagar, aurangabad, dhanbad, amritsar,
navi mumbai, allahabad, prayagraj, ranchi, howrah, coimbatore, jabalpur, gwalior, vijayawada, jodhpur, madurai,
raipur, kota, guwahati, chandigarh, solapur, hubli, mysore, mysuru, tiruchirappalli, trichy, bareilly, aligarh,
tiruppur, gurgaon, gurugram, moradabad, jalandhar, bhubaneswar, salem, warangal, thiruvananthapuram, trivandrum,
bhiwandi, saharanpur, gorakhpur, guntur, bikaner, amravati, noida, jamshedpur, bhilai, cuttack, firozabad, kochi,
cochin, ernakulam, bhavnagar, dehradun, durgapur, asansol, nanded, kolhapur, ajmer, gulbarga, jamnagar, ujjain,
siliguri, jhansi, jammu, mangalore, mangaluru, belgaum, belagavi, gandhinagar, anand, udaipur, shimla, panaji,
puducherry, pondicherry, kozhikode, calicut, thrissur, vellore, tirupati, nellore, mathura, haridwar, rishikesh,
london, manchester, birmingham, glasgow, edinburgh, liverpool, bristol, leeds, new york, los angeles, chicago,
houston, phoenix, philadelphia, san antonio, san diego, dallas, san jose, austin, san francisco, seattle, boston,
washington, miami, atlanta, denver, toronto, vancouver, montreal, ottawa, calgary, sydney, melbourne, brisbane,
perth, adelaide, auckland, wellington, singapore, dubai, abu dhabi, sharjah, doha, riyadh, jeddah, muscat,
kuwait city, manama, paris, berlin, munich, frankfurt, hamburg, madrid, barcelona, rome, milan, amsterdam,
rotterdam, brussels, vienna, zurich, geneva, stockholm, oslo, copenhagen, helsinki, dublin, lisbon, athens,
warsaw, prague, budapest, moscow, istanbul, tokyo, osaka, kyoto, seoul, beijing, shanghai, shenzhen, guangzhou,
hong kong, taipei, bangkok, kuala lumpur, jakarta, manila, hanoi, ho chi minh city, dhaka, karachi, lahore,
islamabad, kathmandu, colombo, cairo, lagos, nairobi, johannesburg, cape town, sao paulo, rio de janeiro,
buenos aires, mexico city, lima, bogota, santiago
"""
)
