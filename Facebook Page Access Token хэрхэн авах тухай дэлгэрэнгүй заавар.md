# Facebook Page Access Token хэрхэн авах тухай дэлгэрэнгүй заавар

Энэхүү зааварчилгаа нь Facebook Page-ийн Access Token-ийг хэрхэн үүсгэх, шаардлагатай зөвшөөрлүүдийг тохируулах болон уг токенийг авто мессеж, авто коммент хариулахад хэрхэн ашиглах талаар алхам алхмаар тайлбарлах болно. Эхлэгчдэд ойлгомжтой байхаар энгийн үг хэллэгээр бичсэн болно.

## 1. Facebook Developer App хэрхэн үүсгэх

Facebook Page Access Token авахын тулд эхлээд Facebook Developer App үүсгэх шаардлагатай. Энэ нь таны аппликейшн Facebook-ийн API-тай харилцах боломжийг олгоно.

**Алхам 1: Facebook Developers сайт руу нэвтрэх**

Вэб хөтөч дээрээ [https://developers.facebook.com/](https://developers.facebook.com/) хаягийг оруулж, Facebook аккаунтаараа нэвтэрнэ үү. Хэрэв та өмнө нь хөгжүүлэгчээр бүртгүүлж байгаагүй бол бүртгүүлэх алхмуудыг дагана уу.

**Алхам 2: Шинэ аппликейшн үүсгэх**

1.  Нэвтэрсний дараа баруун дээд буланд байрлах **"My Apps"** (Миний Аппликейшнүүд) товчийг дарна. 
2.  Дараа нь **"Create App"** (Аппликейшн үүсгэх) товчийг дарна. [1]

**Алхам 3: Аппликейшний төрлийг сонгох**

1.  Аппликейшний төрлийг сонгох хэсэгт таны аппликейшний зорилгоос хамаарч сонголт хийнэ. Жишээлбэл, хэрэв та Facebook Page-тэй харилцах зорилготой бол **"Business"** (Бизнес) эсвэл **"Other"** (Бусад) сонголтыг хийж болно. 
2.  **"Next"** (Дараагийн) товчийг дарна.

**Алхам 4: Аппликейшний дэлгэрэнгүй мэдээллийг оруулах**

1.  **"App Display Name"** (Аппликейшний нэр) хэсэгт аппликейшнийхээ нэрийг оруулна. Энэ нэр нь хэрэглэгчдэд харагдах нэр байх болно.
2.  **"App Contact Email"** (Холбоо барих и-мэйл) хэсэгт хүчинтэй и-мэйл хаягаа оруулна.
3.  **"Create App"** (Аппликейшн үүсгэх) товчийг дарна. Танаас Facebook нууц үгээ дахин оруулахыг хүсэж магадгүй.

Ингэснээр таны Facebook Developer App үүсгэгдэж, та аппликейшнийхээ Dashboard (Хяналтын самбар) руу шилжинэ.

## 2. Facebook Page Access Token хэрхэн авах

Page Access Token нь таны аппликейшнд Facebook Page-ийн өмнөөс үйлдэл хийх (жишээлбэл, пост оруулах, коммент унших, мессеж илгээх) эрхийг олгодог. Page Access Token авахын тулд эхлээд User Access Token авах шаардлагатай.

**Алхам 1: Graph API Explorer ашиглах**

1.  Facebook Developers Dashboard дээрээс **"Tools"** (Хэрэгслүүд) хэсэг рүү орж, **"Graph API Explorer"**-ийг сонгоно уу. Эсвэл шууд [https://developers.facebook.com/tools/explorer/](https://developers.facebook.com/tools/explorer/) хаягаар орж болно.
2.  Graph API Explorer хуудсан дээр **"Facebook App"** (Facebook Аппликейшн) хэсгээс сая үүсгэсэн аппликейшнээ сонгоно.
3.  **"User Token"** (Хэрэглэгчийн Токен) эсвэл **"Get Token"** (Токен авах) товчийг дарж, **"Get User Access Token"** (Хэрэглэгчийн Access Token авах) сонголтыг хийнэ.

**Алхам 2: Шаардлагатай зөвшөөрлүүдийг (Permissions) сонгох**

Хэрэглэгчийн Access Token авахдаа таны аппликейшнд шаардлагатай зөвшөөрлүүдийг сонгох хэрэгтэй. Page-тэй холбоотой үйлдлүүд хийхийн тулд дараах зөвшөөрлүүдийг сонгоно уу:

*   `pages_show_list`: Таны удирддаг Facebook Page-үүдийн жагсаалтыг харах.
*   `pages_read_engagement`: Page-ийн постуудын хариу үйлдэл, коммент, шэйр зэрэг оролцоог унших.
*   `pages_manage_posts`: Page-ийн өмнөөс пост үүсгэх, засварлах, устгах.
*   `pages_read_user_content`: Page-ийн постууд болон бусад контентыг унших.
*   `pages_manage_metadata`: Page-ийн метадатаг удирдах.
*   `pages_messaging`: Page-ийн өмнөөс мессеж илгээх, хүлээн авах.
*   `public_profile`: Хэрэглэгчийн нийтийн профайлын мэдээллийг авах.

Эдгээр зөвшөөрлүүдийг сонгосны дараа **"Get Access Token"** (Access Token авах) товчийг дарна. Facebook танаас эдгээр зөвшөөрлүүдийг баталгаажуулахыг хүсэх болно. Баталгаажуулсны дараа та Graph API Explorer дээр User Access Token-ийг харах болно.

**Алхам 3: User Access Token-ийг Page Access Token болгон солих**

Одоо та User Access Token-ийг ашиглан Page Access Token-ийг авна. Graph API Explorer дээр:

1.  **"User Token"** (Хэрэглэгчийн Токен) эсвэл **"Get Token"** (Токен авах) товчийг дахин дарж, энэ удаад **"Get Page Access Token"** (Page Access Token авах) сонголтыг хийнэ.
2.  Таны удирддаг Page-үүдийн жагсаалт гарч ирнэ. Токен авахыг хүссэн Page-ээ сонгоно уу.
3.  Шаардлагатай зөвшөөрлүүдийг дахин баталгаажуулна. 

Ингэснээр та сонгосон Page-ийн Page Access Token-ийг Graph API Explorer дээр харах болно. Энэ токен нь ихэвчлэн богино хугацаанд хүчинтэй байдаг тул та үүнийг Long-Lived Page Access Token болгон солих шаардлагатай болно. Long-Lived Page Access Token нь 60 хоног хүртэлх хугацаанд хүчинтэй байдаг бөгөөд дахин сунгах боломжтой. [2]

> **⚠️ Хамгийн түгээмэл алдаа — заавал уншина уу.** Graph API Explorer нь **User Token**-ийг **default** (өгөгдмөл) байдлаар харуулдаг. Хэрэв та энэ User token-ийг андуурч хуулж авбал бот "чимээгүй" болно: token нь хүчинтэй мэт харагдавч мессеж илгээх бүрд `GraphMethodException code:100 subcode:33 "Object with ID 'me' does not exist"` алдаа гарна (бот `me/messages` рүү илгээдэг тул `me` нь заавал Page байх ёстой). **Шийдэл:** "User or Page" талбарыг заавал **"Page Access Tokens → Magic Financial Group"** болгон сонгож, тэндээс гарсан токенийг ашиглана уу — хүн (таны нэр) биш, Page-ийн токен байх ёстой. Шалгах арга нь доорх §5-д бий.

**Long-Lived Page Access Token авах (Нэмэлт алхам):**

Graph API Explorer-ээс авсан богино хугацааны Page Access Token-ийг ашиглан дараах Graph API дуудлагыг хийж Long-Lived Page Access Token авна:

```
GET /me/accounts?access_token={short-lived-user-access-token}
```

Энэ дуудлага нь таны удирддаг Page-үүдийн жагсаалтыг буцаана. Жагсаалт дотор Page бүрийн `access_token` талбарт Long-Lived Page Access Token байх болно. [2]

## 3. Page удирдахад шаардлагатай зөвшөөрлүүдийг хэрхэн тохируулах

Дээр дурдсан `pages_messaging`, `pages_read_engagement`, `pages_manage_posts` зэрэг зөвшөөрлүүдийг та аппликейшн үүсгэх явцад болон Graph API Explorer ашиглан User Access Token авахдаа сонгосон байх ёстой. 

**App Review (Аппликейшний хяналт):**

Хэрэв таны аппликейшн олон нийтэд нээлттэй байх эсвэл Facebook-ийн API-ийн тодорхой функцуудыг ашиглах бол Facebook-ийн App Review процессыг дамжих шаардлагатай болно. Энэ нь таны аппликейшн Facebook-ийн бодлого, удирдамжийг дагаж мөрдөж байгаа эсэхийг баталгаажуулах зорилготой. App Review-д хамрагдахын тулд таны аппликейшн ямар зорилготой, ямар зөвшөөрлүүдийг яагаад ашиглах шаардлагатай байгааг дэлгэрэнгүй тайлбарлах хэрэгтэй. [3]

## 4. Энэ токенийг авто мессеж болон авто коммент хариулахад хэрхэн ашиглах талаар товч тайлбар

Page Access Token-ийг ашиглан та Facebook Page-ийн өмнөөс дараах автоматжуулсан үйлдлүүдийг хийх боломжтой:

*   **Автомат мессеж илгээх/хариулах:** `pages_messaging` зөвшөөрлийг ашиглан та Page-д ирсэн мессежүүдэд автомат хариу илгээх эсвэл тодорхой нөхцөл байдлын үед хэрэглэгчдэд мессеж илгээх боломжтой. Үүнийг ихэвчлэн Messenger Platform API-аар дамжуулан хийдэг.
*   **Автомат коммент хариулах:** `pages_manage_posts` болон `pages_read_engagement` зөвшөөрлүүдийг ашиглан та Page-ийн постууд дээр ирсэн комментуудыг уншиж, тодорхой түлхүүр үгс эсвэл нөхцөл байдлын дагуу автомат хариу коммент үлдээх боломжтой. 

Эдгээр үйлдлүүдийг хийхийн тулд та Facebook Graph API-тай харилцах код бичих шаардлагатай болно. Ихэвчлэн Python, Node.js, PHP зэрэг програмчлалын хэлүүдийг ашиглан Facebook SDK-г нэгтгэж, API дуудлагуудыг хийдэг.

## 5. Токен дуусах / бот чимээгүй болоход яах вэ (Runbook)

Page Access Token нь ойролцоогоор 60 хоногт хүчинтэй (жишээ нь **2026-09-04** хүртэл), эсвэл Facebook нууц үг солигдоход хүчингүй болдог. Ийм үед бот мессежийг хүлээж авсаар байгаа ч хариу **илгээж чадахгүй** болно. Render логт `Send API FAILED ... "code":190` (токен хүчингүй) эсвэл `"code":100 ... "subcode":33` (User token андуурсан) гэсэн алдаа харагдана.

Шинэчлэх алхмууд:

1. **Дээрх 2-р хэсгийн дагуу шинэ Page токен ав** (User биш — §2, §3, дээрх ⚠️ анхааруулгыг санана уу). Дараа нь Access Token Debugger (<https://developers.facebook.com/tools/debug/accesstoken/>) дээр токеноо оруулж **"Extend Access Token"** дарж Long-Lived болгоно.
2. **Render дээр солино:** <https://dashboard.render.com> → **magicbot** сервис (⚠️ өөр сервис биш — `gmcbot` устсан) → зүүн талын **Environment** → `FACEBOOK_ACCESS_TOKEN` мөрийн утгыг шинэ токеноор солино → доор нь **"Save, rebuild, and deploy"** товч дарна. ~2 минут deploy хийгдэнэ.
3. **Шалгах:** deploy дууссаны дараа Render → **Shell** таб дээр дараах командыг буулгаж ажиллуулна (энэ нь токенийг таны нэр рүү биш, Page рүү зөв заасан эсэхийг шалгана, юу ч өөрчлөхгүй):
   ```
   python -c "import os,requests;print(requests.get('https://graph.facebook.com/v18.0/me',params={'access_token':os.environ['FACEBOOK_ACCESS_TOKEN']}).text)"
   ```
   Хариу нь `{"name":"Magic Financial Group","id":"123001937756085"}` байвал **зөв** — бот дахин ажиллаж эхэлнэ. Хэрэв хүний нэр гарвал та дахин User token хуулсан байна → 1-р алхмыг давтана уу.
4. **Огт дуусдаггүй токен (сонголт):** §2-ын нэмэлт алхмын дагуу `/me/accounts`-оос авсан Page токен нь never-expiring байдаг тул 60 хоног тутам дахин шинэчлэх шаардлагагүй болдог.

**Сануулга:** Токен дуусах хугацаа (~8 сарын сүүл) ойртохоос өмнө шинэчлэхээ календарьт тэмдэглэ. Дуусвал бот шууд чимээгүй болно.

## Ашигласан материал

[1] Create an App - App Development with Meta. (n.d.). Retrieved from [https://developers.facebook.com/docs/development/create-an-app/](https://developers.facebook.com/docs/development/create-an-app/)
[2] Access Tokens for Meta Technologies | Developer Documentation. (n.d.). Retrieved from [https://developers.facebook.com/documentation/facebook-login/guides/access-tokens](https://developers.facebook.com/documentation/facebook-login/guides/access-tokens)
[3] Permissions Reference - App Development with Meta. (n.d.). Retrieved from [https://developers.facebook.com/docs/permissions/](https://developers.facebook.com/docs/permissions/)
