\set ON_ERROR_STOP on
GRANT CONNECT ON DATABASE :"DBNAME" TO :"reader";
GRANT USAGE ON SCHEMA public TO :"reader";
GRANT SELECT (id, name, price, is_available, category_id) ON public.menu_items TO :"reader";
GRANT SELECT (id, name) ON public.categories TO :"reader";
GRANT SELECT (id, balance) ON public.users TO :"reader";
GRANT SELECT (id, user_id, status, total_cost) ON public.orders TO :"reader";
