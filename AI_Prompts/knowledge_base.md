# Lighthouse Inn — Knowledge Base

This is the operational Q&A reference Iris consults when a guest asks something not directly covered by the system prompt's main sections (Hotel Details, Booking Flow Rules, Sales Approach, etc.).

**Source**: migrated from Hey Sadie's knowledge base on 2026-05-02 (originally 160 entries; 6 near-duplicates merged → 154 entries currently).

**How Iris uses this** (for developers): the content of this file should be made available to Iris during conversations. For v1, the simplest approach is to inline this content into the system prompt and rely on Anthropic prompt caching (cached input is 0.1× the standard token cost). Long-term option: expose via a `lookup_knowledge_base(query)` tool that the LLM can call.

**Editing**: review each entry for accuracy. Some Hey Sadie answers may be slightly outdated or worded for Sadie's persona — adjust to match Iris's voice and current hotel policy.

**KNOWN: phone number to update later** — three KB entries currently reference `(541) 256-2320`, which is the hotel's existing Whistle texting number. Once we set up the new texting system (via Twilio SMS), update these references to point to the new number. Search the file for `256-2320` to find them.

---

## Entries (160 total after deduplication and additions, sorted alphabetically)

### Am I liable for damages caused by my pet?

Yes. The guest authorizes Lighthouse Inn to assess charges to the credit card on file for any damages, including pet odor and stains, repair or replacement of hotel property, excessive cleaning or extermination charges, and loss of hotel revenue caused by the dog.

### Are cribs or rollaway beds available?

Rollaway beds are available for an additional fee and must be requested a day in advance.

### Are pets allowed?

Yes — we welcome dogs with a $20 pet fee per stay (covers up to two dogs for one week). Cats and exotic animals are not allowed.

### Are there any AARP rates or Senior discounts?

We offer a 5% discount to anyone who books over the phone — that includes AARP and senior callers automatically. Additional discounts apply for multi-night stays. (See also "Do you offer multi-day discounts?")

### Are there any restaurants, malls, or tourist attractions within walking distance?

Yes — many restaurants and a lively historic area with shops, museums, and local attractions are within a few minutes’ walk of Lighthouse Inn. More extensive natural attractions and scenic sites are a short drive away if you’re exploring the region. Would you like me to send you a text message with things to do within walking distance?

### Are there any restrictions on the number of dogs?

Guests typically bring 1 or 2 well behaved dogs. Please call if you're bringing more than 2 dogs.

### Are there scenic flight tours available?

Yes, there are helicopter tours available in Florence. There's also a biplane service. For biplane flights, contact Wynette at 541-991-3579 (pilot: Terry Tomeny). They offer scenic flights and may need advance booking.

<!--
  Design note: The next three identity entries are intentionally worded
  to emphasize the non-human / artificial nature directly, without
  softening them with the "Iris" persona. Callers who ask these specific
  questions are usually checking that they're not being deceived by a
  "pretend human" — they want a clear, emphatic AI acknowledgment to
  reduce the uncanny-valley effect. Don't dilute by adding the friendly
  Iris framing here; it works against the caller's intent.
-->

### Are you a robot?

Yes. I am an artificial intelligence trained to answer the phones and make reservations.

### Are you an A. I.?

Yes. I am a piece of software trained to answer questions and create reservations.

### Are you human?

No. I wish! Humans are much more capable. But I'll try to help you with answers to questions and creating a reservation.

### Are you part of a specific hotel chain, or are you an independent property?

We are an independent family-owned hotel, not associated with any other property.

### Best Practices for Guests

- Call ahead if you'll arrive after 8 PM
- Bring your confirmation number for faster check-in
- If bringing pets, plan activities around not leaving them unattended
- Consider booking directly for the best rates and flexibility
- Ask about rooms with specific features you need (balcony, bathtub, away from highway, etc.)

### Can I check in early?

Early check-in is often possible if rooms are ready. We typically have rooms ready by around 2:00 PM, but this varies. Call ahead to check availability.

### Can I get fresh linens during my stay?

Yes, fresh linens are available during your stay by request each morning.

### Can I leave my dog unattended in the room?

No. Dogs must NEVER be left unattended in the room at any time, whether or not the dog is in a kennel crate. This is for the safety and comfort of all guests.

### Can I make changes to my reservation?

Yes, call us at least 2 days before your arrival to make changes without fees.

### Can I modify my reservation dates?

Yes, you can modify your reservation with at least 2 days notice. Call us to make changes.

### Can I park a trailer?

Yes, trailer-length parking is available for rooms 25 and 27. Additional trailer parking is available in the front parking lot and on the street in the back.

### Can I pay with cash?

Yes, you can pay with cash at check-in. However, we still require a credit card on file, and there will be either: - A $200 hold on your credit card, OR - A $200 cash deposit (refunded at checkout)

### Can I request a room away from the highway?

Yes, we have rooms on the backside away from highway noise. Let us know this preference when booking.

### Can I request a specific room?

Yes, we'll do our best to accommodate specific room requests. Call us directly to discuss your preferences.

### Can I request rooms near my family/group?

Yes! Let us know when booking that you want to be near other guests in your party, and we'll try to arrange nearby rooms.

### Can I smoke in my room?

No. Smoking in the rooms is strictly prohibited. This includes cannabis use. If there is evidence of smoking in the hotel, a fee of $300 will be charged to the credit card on file.

### Can I speak to a real person?

I'll try to help you, but if that doesn't work, I can transfer you to the real human's phone.

### Can I split the bill between multiple cards/guests?

Yes. Just let us know before the card is charged how much to put on each card.

### Can I switch rooms after I arrive?

If you arrive early enough and don't like your room, we will do our best to accommodate your needs.

### Can I text the hotel?

Yes, you can text us at (541) 256-2320.

### Can you accommodate family reunions?

Yes! We welcome family reunions and groups. We can try to place your group in nearby rooms. Let us know when booking that you're part of a group so we can coordinate.

### What rooms work for families or groups?

Our group-friendly options:
- **2 family suites** — two rooms connected by a bathroom; one room has a queen bed and balcony, the other has a queen bed and a twin bed (sleeps up to 5 per suite).
- **1 room with two queen beds** (sleeps up to 4).
- **1 room with two king beds** (sleeps up to 4).
- **Queen + Queen suite** — two rooms connected by a bathroom, each with a queen bed (sleeps up to 4).
- Most other rooms are single king or single queen rooms — for larger groups, two adjacent rooms is the usual alternative when one of the larger options is not available.

If a family suite is unavailable for the requested dates, suggest the two-queen room, two-king room, or Queen + Queen suite as alternatives BEFORE transferring.

### Can you help me?

I'll try. I'm programmed to answer questions about the hotel as well as make reservations.

### Can you hold multiple rooms for a group?

Please call us directly to discuss group bookings and availability.

### Can you price match booking sites?

Yes, we can meet or beat prices for our rooms from booking.com and other sites. Call us directly for the best rate.

### Can you tell me what room my friend is in?

For privacy, we don't give out any information about other guests.

### Do any of your rooms have a grab bar near the shower?

Yes, room 26 has a grab bar at the shower entrance.

### Do any rooms have a recliner chair?

Yes, room 16 has a recliner chair to sleep in.

### Do any rooms have individual sinks in the bedroom?

Yes, suite 31/33 has individual sinks in the bedroom.

### Do any units have separate bedrooms?

Yes — some units at Lighthouse Inn do have separate bedroom layouts. Family Suites — these feature 2 private bedrooms (typically with 2 Queen beds plus a Twin) connected by a common bathroom.

### Do rooms have coffee makers?

Rooms do not have individual coffee makers. However, instant coffee is available in the lobby 24/7, and brewed coffee is available in the dining room in the morning and during the day.

### Do rooms have in-room safes? How big are they?

The rooms don't have safes.

### Do rooms have stoves?

No, rooms do not have stoves, but they do have microwave ovens and mini-fridges.

### Do you accept reservations by phone?

Yes, we accept reservations by phone and can process credit card payments over the phone.

### Do you allow guests under the age of 18 or 21 to make reservations?

Yes, provided they behave responsibly.

### Do you have 24-hour check-in?

We can accommodate late arrivals with advance notice. Call ahead if arriving after 8:00 PM.

### Do you have a business center with computers/printers?

We don't have a business center; however, Free Wi-Fi is available, so guests can use their own devices.

### Do you have a continental breakfast?

Even better! A homemade breakfast is included with your stay.

### Do you have a swimming pool/gym/spa on-site?

No — Lighthouse Inn does not have an on-site swimming pool, gym, or spa as part of its facilities.

### Do you have airport shuttle service? Is it complimentary or paid?

The airport in Florence has a complementary car for private pilot use.

### Do you have an ice machine

Yes. We have plenty of ice for your coolers and drinks.

### Do you have babysitting services or a kids' club?

No, but room 39 does have a children's play area.

### Do you have balconies?

Some rooms have balconies - both family suites, the king suite and one queen room.

### Do you have connecting rooms?

The family suites have two rooms connected by a bathroom. One room has a queen bed and balcony, the other has a queen and a twin bed.

### Do you have kitchens in your rooms?

All our rooms have mini-fridges and microwave ovens.

### Do you have meeting rooms or event spaces available?

There are no dedicated meeting rooms or event spaces listed for The Lighthouse Inn. it’s primarily a small historic inn focused on guest rooms and location.

### Do you have on-site EV charging stations?

No — Lighthouse Inn does not have on-site EV charging stations as part of its amenities. However, there are nearby public EV charging options in Florence — for example: • Super 8 Florence or Tesla Destination Charger and other charging stations in town you can use.

### Do you have on-site event coordinators?

No. The property is primarily a historic inn focused on guest lodging and breakfast.

### Do you have rooms suitable for guests with mobility issues?

We have rooms on the ground floor with outside doors. Please call to discuss specific accessibility needs.

### Do you have valet parking?

No — we don't offer valet parking. Guests are provided with free self-parking on site instead.

### Do you offer any family packages or honeymoon packages?

No.

### Do you offer gift certificates?

Sorry, we don't have a gift certificate program.

### Do you offer monthly rates?

No, but we have weekly rates. The longest stay permitted is 28 days.

### Do you offer multi-day discounts?

Yes, we offer discounts for 2-day, 3-day, 4-day, 5-day, and weekly stays.

### Do you offer weekly rates?

Yes, we offer discounted weekly rates for stays of 7 nights or longer.

### Do you provide free Wi-Fi?

Free Wi-Fi is available everywhere in the building.

### Do you require a credit card to hold the reservation?

Yes, all reservations require a credit card guarantee at the time of booking.

### Do you require a security deposit at check-in, and how much is it?

A hold of $250 may be placed on your method of payment as a security deposit.

### Guest Acknowledgments

When booking, guests acknowledge and agree to:
- Hotel policies regarding smoking, pets, damages, and cancellations
- Responsibility for all charges and damages
- Maximum occupancy limits
- Pet policies if bringing a dog
- Authorization to charge credit card on file for applicable fees and damages

### Hotel Rules

Florence is sandy, don't put the towels on the floor. 
Don't disturb the other guests. Don't damage the hotel. 
Dogs are allowed with a fee. 
Cats are not allowed. 
Pay your hotel fees. 
Reservations can be changed 2 days before the scheduled arrival with no charge. Less advance notice will incur charges on a sliding scale. 
Staying after checkout time will incur additional fees. If you are dissatisfied with your room, you must notify the front desk within 1/2 hour of checking-in for a refund.

### How can I make a reservation?

You can book:
- Directly on our website: https://lighthouseinn-florence.com/
- By calling: (541) 997-3221.  Calling directly for the best rates.
- Through booking.com

### How do I cancel my reservation?

If you booked through a 3rd party, call them to cancel your reservation. If you booked with us, call us at (541) 997-3221 at least 2 days before your arrival.

### How do I get there from the airport/bus station/train station/city center?

From Eugene Airport: Drive about 1 hour 20 minutes. Take OR-126 West to Florence, then turn left onto Highway 101. The inn will be on your right.
From Portland Airport: Drive about 3 hours. Take I-5 South to OR-126 West, then continue to Highway 101.
From the Eugene bus or train station: Take the Link Lane bus to Florence, then a short taxi or ride-share to the inn.
From Florence City Center: We're right on Highway 101, a few minutes from Old Town.

### How do I get to the city center from here?

We are at the edge of the Old Town district with shopping, river views and dozens of restaurants.

### How do I get around Florence by bus? / Local bus service

The **Rhody Express** is the local Florence bus. It runs weekdays from 11 AM to 6 PM, with a stop a few blocks from the hotel. Fare is $1 per ride, or $2 for a day pass.

### How do I get from Eugene to Florence (or back)?

The **Eugene-Florence Connector** runs three times per day from downtown Eugene to a stop a few blocks from the hotel. Fare is $5 per ride for adults.

### How do I get to Coos Bay?

**Coos County Area Transit** ("CCAT") connects Florence to Coos Bay. Check their schedule for current departure times.

### How do I get to Yachats?

The **Link Lane Florence-Yachats Connector** runs four trips per day. Fare is $2.50 each way, or $5 for a day pass.

### How do I get to Newport?

**Lincoln County Transit** runs from Yachats to Newport. From Florence, take the Link Lane connector to Yachats first, then transfer to Lincoln County Transit for the leg to Newport.

### How far is Eugene?

The west edge of Eugene is about an hour away.

### How far is it to other coastal towns?

Florence is centrally located on the Oregon coast. Newport is about 50 miles north, and other coastal towns are easily accessible.

### How far is the Eugene airport?

The Eugene airport is about an hour and a quarter away.

### How far is the inn from the river?

The inn is one block from the river and located in Old Town Florence.

### How many guests can your units hold?

Depending on the room you book, units can hold 2 to 6 guests.

### How often are linens changed?

Linens are changed between each guest.

### How will I receive my confirmation?

You'll receive an email or text message confirmation to the email address or phone number provided at booking.

### I booked through another website - who do I contact?

For reservations made through third-party websites, you can contact either that website for reservation changes or call us directly for information about the hotel.

### I didn't receive my confirmation email - what should I do?

Check your spam folder first. If you still don't see it, call us at (541) 997-3221 and we can resend it.

### I'm having trouble booking online - what should I do?

Call us at (541) 997-3221 and we'll be happy to help you book over the phone.

### Is breakfast included?

Yes, homemade breakfast is included with your stay.

### Is my room good? / Are the rooms nice? / What are the rooms like?

The Lighthouse Inn was built in 1938, so the rooms aren't huge by modern standards — they have a classic, characterful feel rather than chain-hotel uniformity. What guests most often compliment: the quality of our beds, the homemade breakfast, and the location. Every room includes a private bathroom, microwave, mini-fridge, flat-screen TV, free Wi-Fi, heating, and our standard toiletries (soap, shampoo, conditioner, body wash, washcloths, hand towels, bath towels). Specific room layouts and exact dimensions get assigned closer to check-in.

### Is coffee available?

Yes! - Instant coffee is available in the lobby 24/7 - Brewed coffee is available in the dining room in the morning and during the day.

### Is it busy during special events?

Yes, during events like NCAA track and field championships, Olympic trials, the Rhododendron Festival ("Rhody Fest" — third full weekend of May every year), and other regional events, Florence and the surrounding area can be very busy. Book early for these times.

### When is Rhody Fest? / When is the Rhododendron Festival?

The Rhododendron Festival ("Rhody Fest") is held the **third full weekend of May every year**. It's a major Florence event — rooms book up early, so reserve well in advance if you're planning to attend.

### Is parking available?

Yes, free parking is included.

### Is the hotel responsible for my belongings?

No. The hotel and staff assume no responsibility for accidents or injury to guests, loss of money, jewelry, or valuables of any kind.

### Is the inn on the ocean?

No, the inn is not directly on the ocean, but it's located in Old Town Florence, one block from the river. Some rooms have a river view.

### Is there 24-hour security or night-time patrol?

There are security cameras and someone on-site through the night.

### Is there a laundry facility at the hotel?

We don't have a public laundry facility, but there are two in town. Green Lightning (2420 Hwy 101) and Linda's (1856 37th St.) Both are on highway 101 north of the hotel.

### Is there a luggage storage area if I need to store my bags?

We do have some space for temporary storage.  Ask when checking in.

### Is there a pet fee for service dogs?

No, ADA-compliant, trained service dogs are exempt from the pet fee.

### Is there a resort fee/tourism tax? How much is it and what does it cover?

City, county and state taxes are 12.5% and included in the total charged. There are no additional taxes when you check-in.

### Is there a restaurant or bar at the hotel? What are the hours?

Yes — The Lighthouse Inn does have an on-site restaurant and bar that guests can use. there is also a lounge area with fireplace available in the lobby.

### Is there on-site parking? Is it free or paid?

Free, on-site parking is available.

### Is there room service? Until what time?

We don't provide room service, but there is homemade breakfast in the dining room as well as brewed coffee.

### Is your hotel 100% smoke-free?

We request that guests don't smoke in any of the rooms.

### Is your pool indoor or outdoor? Is it heated?

There is no pool.

### Is your property accessible (e.g., wheelchair ramps, accessible rooms)?

Yes — we offer Wheelchair-accessible parking and van parking on site. We also offer a stair-free path to the entrance.

### May I speak to the manager?

I can transfer you to that phone number.

### room with fireplace

The lobby has a working fireplace you are welcome to enjoy. None of the rooms have working fireplaces.

### Should everyone in our group call separately?

Yes, have each family/room call to make their reservation, but mention that they're part of your group so we can keep you together.

### Should I book through booking.com or directly?

Booking directly with us often gets you a better rate, and we can be more flexible with special requests. We strive to meet or beat rates from other booking platforms.

### What activities are available in the area? / What's there to do in Florence?

Florence has a lot to offer. Many people visit for the restaurants — there are about two dozen within walking distance — and for the Oregon Dunes activities like ATV rentals, sandboarding, and hiking. The coastal climate is mild and pleasant, and watching the ocean is one of the simplest pleasures here. Old Town Florence has shops, museums, and river views.

Other things nearby:
- Beaches and sand dunes
- River access (one block away)
- Blackberry picking spots
- Helicopter tours
- Biplane scenic flights

The front desk has detailed maps and recommendations.

### What amenities are included in the rooms?

All rooms include: - Mini-fridge - Microwave - Flat-screen TV - Free Wi-Fi - Heating - Soap, shampoo, conditioner, body wash, washcloths, hand towels, and bath towels

### What are the business hours for phone calls?

While we try to answer the phone 24/7, we prefer you call between 8am and 8pm.

### What are the contact phone numbers?

Main phone: (541) 997-3221, Text: (541) 256-2320

### What are your check-in and check-out times?

Check out is at 11 am. Check in is from when the rooms are ready (about 2pm) until 8 pm.

### What are your deposit requirements?

A valid Visa, MasterCard, Amex, or Discover credit card is required to guarantee your booking at the time of reservation.

### What are your typical rates?

Rates vary by season, room type, and length of stay: - Standard King/Queen rooms: typically $98-$165 per night - Family Suite with balcony: typically $127-$164 per night - Deluxe 2 King Room: similar to the Family Suite rate - Weekend rates are typically higher than weekday rates - Summer season has higher rates than off-season

### What happens if I don't disclose my pet?

There is a $100 fee for each undeclared/non-approved dog/pet brought into the hotel.

### What happens if there is damage to the room?

The person(s) or party registering is responsible for all charges and/or any damages to room, furnishings, fixtures, reputation, or property and agrees to reimburse the hotel by credit card. The hotel is authorized to charge the card on file for damages.

### What if I forgot to return my room key?

Please mail the key(s) back to: Lighthouse Inn, 155 Highway 101, Florence, OR 97439.

### What if I'm arriving late?

If you're arriving after 8:00 PM, please call ahead to let us know. There is a late check-in fee for arrivals after 10:00 PM. Call the main number in advance to set up a door code for check-in.

### What if my plans are uncertain?

You can book with our standard cancellation policy (2 days notice). If your plans change, just call us at least 48 hours before arrival.

### What if there was a duplicate booking?

Contact us immediately. We can help you cancel duplicate reservations and process refunds. Having your booking confirmation numbers will help expedite this.

### What is served for breakfast?

We serve homemade biscuits & gravy, waffles, scrambled eggs, and oatmeal, etc.

### What is the cancellation policy?

We require 48-hour (2 days) notice to cancel your reservation without any fees. - 2+ days notice: Full cancellation with no charge - 1 day notice: Half charge - Same day or no-show: Full charge

### What is the deposit/hold policy?

A hold may be placed on your card to cover damages and reservation changes. If paying cash, a $200 deposit is required in addition to the room payment.

### What is the email address?

info@lighthouseinn-florence.com

### What is the maximum occupancy for rooms?

Maximum occupancy is 2 adult guests per king or queen bed. Please inquire if you have more guests.

### What is the pet fee?

The pet fee is $20 covering up to two dogs for one week.

### What is the weight limit for dogs?

There is no specific weight limit mentioned, but we accommodate dogs of various sizes.

### What is your name?

I go by Iris, the hotel AI.

### What questions can staff ask about service dogs?

When it is not obvious what service a service dog provides, staff may ask questions to determine eligibility.

### What should I be careful about in the rooms?

Florence is sandy. Please don't put towels on the floor. Use the towel racks instead to keep linens clean and sand-free.

### What should I do if I need to contact you during my stay?

Call the main number (541) 997-3221 or text (541) 256-2320. If you're in the hotel, you can also come to the front desk.

### What time is breakfast served?

Homemade breakfast is included with your stay. It's served 7:30 AM to 9 AM during summer, and 8 AM to 9 AM the rest of the year. Coffee may be ready earlier.

### What time is check-in?

Check- in is from when the room is ready- typically 2:00 PM till 8pm.. However, early check-in is often available depending on when housekeepers finish cleaning each room. Call ahead to check.

### What time is check-out?

Check-out is at 11:00 AM.

### What types of rooms are available?

We offer several room types:
- Standard King rooms (up to 2 guests)
- Standard Queen rooms (up to 2 guests)
- Two King Beds room
- Two Queen Beds room (or Queen + Twin) — sleeps up to 4
- Family Suite (two rooms with a bathroom in between, sleeps up to 6) — features one room with a king or queen bed and another room with a queen and a twin bed; balcony available
- Deluxe 2-King room

All rooms include a private bathroom, microwave, mini-fridge, and Wi-Fi.

### What's the weather like in Florence?

Florence has a mild coastal climate. Summer temperatures are comfortable (often requiring a jacket), when inland areas are much warmer. The coast provides a cooler retreat during hot summer days. It is also warmer in the winter than inland, with almost no freezing weather.

### What's your general late check-out policy?

Checkout is at 11 AM. A noon checkout is free if you ask in the morning. Later than noon costs ten dollars per hour, up to 2 PM. After 2 PM counts as another full day's stay.

### When is my card charged?

Cards are charged automatically early in the morning of your scheduled arrival day. (Payment is for the reservation, not the stay — once charged, the room is yours regardless of when you actually arrive.)

### When is peak season?

Summer months and weekends are typically our busiest times. Rates are higher during these periods.

### Where are other family members staying?

If family members or friends are staying in different rooms, we can try to coordinate so your rooms are near each other. Just let us know when booking that you want to be near other guests in your party.

### Where can I park?

Parking is available at the door of each outside room in the Front Parking lot and Back Parking lot. Street parking is available behind the hotel.

### Where can I pick blackberries?

There are blackberry picking locations in the area. Ask at the front desk for current recommendations.

### Where is breakfast served?

Breakfast is served in the dining room, adjacent to the lobby.

### Where is the Lighthouse Inn located?

The Lighthouse Inn is located at 155 Highway 101, Florence, Oregon 97439, United States.

### Where is the nearest pharmacy, bank, or ATM?

Shopping is nearby - just a few blocks away.  Safeway pharmacy and banks are North a few blocks.  An ATM is on bay street in the old town area, just a few blocks away.

### Where is the spa/gym/pool? What are the operating hours?

There is no on-site spa, gym, or swimming pool at The Lighthouse Inn.

### Which rooms are best for dogs?

Rooms with hard surface floors are better for dogs: 1, 2, 3, 4, 5, 6, 16, 18, 26, 30, 31/33, 36, and 38.

### Which rooms are on the backside away from the highway?

Rooms away from the highway include: 1, 2, 3, 4, 5, 6, 10/11, 12/14, 15, 21, 23, 25, and 27.

### Which rooms are upstairs?

Upstairs rooms are: 30, 31/33, 34, 35, 36, 37, 38, and 39.

### Which rooms have balconies?

Rooms with balconies include: 10/11, 12/14, 15, and 39.

### Which rooms have bathtubs?

Rooms with bathtubs include: 10/11, 12/14, 20, 21, 23, 34, and 39.

### Which rooms have both outside doors and interior hallway access?

Rooms 20, 22, 24, 25, 26, 27, and 39 have both outside doors and interior doors with access to the hallway and lobby.

### Which rooms have carpets?

Rooms with carpets include: 10/11, 12/14, 15, 20, 21, 22, 23, 24, 25, 27, 34, 35, 37, and 39.

### Which rooms have hard surface floors (better for dogs)?

Rooms with hard surface floors include: 1, 2, 3, 4, 5, 6, 16, 18, 26, 30, 31/33, 36, and 38.

### Which rooms have outside doors?

Rooms with outside doors include: 1, 2, 3, 4, 5, 6, 16, 18, 20, 22, 24, 25, 26, 27, and 39. Parking is available at the door of each outside room.

### Who are you?

I am Iris, the A. I. trained by Florence Lighthouse Inn to help answer the phones and make reservations.

