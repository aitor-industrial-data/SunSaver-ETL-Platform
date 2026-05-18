aws ecr get-login-password --region eu-south-2 | docker login --username AWS --password-stdin 610140802215.dkr.ecr.eu-south-2.amazonaws.com
docker build -t sunsaver-etl .
docker tag sunsaver-etl:latest 610140802215.dkr.ecr.eu-south-2.amazonaws.com/sunsaver-etl:latest
docker push 610140802215.dkr.ecr.eu-south-2.amazonaws.com/sunsaver-etl:latest
